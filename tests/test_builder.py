from __future__ import annotations

import subprocess

import pytest

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.code_adapters import WRITE_TOOLS
from orac.models import CapabilityRequest, CapabilityStatus, Task


def _init_repo(path) -> None:
    (path / ".orac").mkdir()
    # mirror the real repo: broker state lives under .orac and is never committed
    (path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)


def _store(path) -> BrokerStore:
    return BrokerStore(path).init()


# --- privilege separation (design §4.6) ---------------------------------------


def test_only_builder_holds_write_grants() -> None:
    broker = ToolBroker.from_manifests()

    assert WRITE_TOOLS <= broker.grants["Builder"]
    for reviewer in ("Intent", "Optimiser", "Simples", "Efficiency", "Orchestrator"):
        assert not (WRITE_TOOLS & broker.grants.get(reviewer, frozenset())), (
            f"{reviewer} must not hold any write grant"
        )


def test_reviewer_write_is_denied_at_the_broker(tmp_path) -> None:
    _init_repo(tmp_path)
    broker = ToolBroker.from_store(_store(tmp_path), repo_root=tmp_path)
    task = Task(title="x")

    result = broker.request(
        CapabilityRequest(
            agent="Intent",
            tool="repo.write_file",
            task_id=task.id,
            args={"path": str(tmp_path / "x.py"), "content": "nope"},
        ),
        task,
    )

    assert result.status is CapabilityStatus.DENIED


# --- the code-creation loop ---------------------------------------------------


def test_builder_branch_write_commit_loop(tmp_path) -> None:
    _init_repo(tmp_path)
    broker = ToolBroker.from_store(_store(tmp_path), repo_root=tmp_path)
    task = Task(title="build something")

    def build(tool: str, **args):
        return broker.request(
            CapabilityRequest(agent="Builder", tool=tool, task_id=task.id, args=args), task
        )

    assert build("git.create_branch", name="feature").status is CapabilityStatus.ALLOWED
    wrote = build("repo.write_file", path=str(tmp_path / "pkg" / "hello.py"), content="VALUE = 1\n")
    assert wrote.status is CapabilityStatus.ALLOWED
    assert (tmp_path / "pkg" / "hello.py").read_text() == "VALUE = 1\n"

    found = build("repo.search", query="VALUE = 1")
    assert found.data["count"] >= 1

    committed = build("git.commit", message="add hello")
    assert committed.status is CapabilityStatus.ALLOWED
    assert len(committed.data["sha"]) == 40

    assert build("git.status").data["changes"] == []


def test_builder_runs_tests(tmp_path) -> None:
    _init_repo(tmp_path)
    broker = ToolBroker.from_store(_store(tmp_path), repo_root=tmp_path)
    task = Task(title="verify")
    test_file = tmp_path / "test_sample.py"
    test_file.write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")

    result = broker.request(
        CapabilityRequest(
            agent="Builder",
            tool="repo.run_tests",
            task_id=task.id,
            args={"target": str(test_file)},
        ),
        task,
    )

    assert result.status is CapabilityStatus.ALLOWED
    assert result.data["passed"] is True


def test_push_runs_unattended_and_lands_in_review_queue(tmp_path) -> None:
    _init_repo(tmp_path)
    remote = tmp_path.parent / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)], cwd=tmp_path, check=True, capture_output=True
    )
    store = _store(tmp_path)
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(title="ship it")

    def build(tool: str, **args):
        return broker.request(
            CapabilityRequest(agent="Builder", tool=tool, task_id=task.id, args=args), task
        )

    (tmp_path / "f.py").write_text("X = 1\n", encoding="utf-8")
    assert build("git.commit", message="add feature").status is CapabilityStatus.ALLOWED

    # Review-after: push does NOT park — it runs and queues a review entry.
    pushed = build("git.push", root=str(tmp_path), remote="origin", branch="main")
    assert pushed.status is CapabilityStatus.ALLOWED
    assert store.list_pending() == []

    queue = store.list_notifications()
    assert [n.tool for n in queue] == ["git.push"]
    assert "Pushed" in queue[0].message
    # the remote really has the branch
    refs = subprocess.run(
        ["git", "branch", "--list", "main"], cwd=remote, capture_output=True, text=True
    )
    assert "main" in refs.stdout

    # human acks the review; the queue drains
    store.ack_notification(queue[0].id)
    assert store.list_notifications() == []


def test_reviewed_not_ok_rolls_back_with_revert(tmp_path) -> None:
    _init_repo(tmp_path)
    store = _store(tmp_path)
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(title="feature, then rollback")

    def build(tool: str, **args):
        return broker.request(
            CapabilityRequest(agent="Builder", tool=tool, task_id=task.id, args=args), task
        )

    (tmp_path / "f.py").write_text("X = 1\n", encoding="utf-8")
    build("git.commit", message="base")
    (tmp_path / "f.py").write_text("X = 2\n", encoding="utf-8")
    feature_sha = build("git.commit", message="feature").data["sha"]
    assert (tmp_path / "f.py").read_text() == "X = 2\n"

    # Reviewer says "not ok" -> one-step undo, no history rewrite.
    reverted = build("git.revert", sha=feature_sha)
    assert reverted.status is CapabilityStatus.ALLOWED
    assert (tmp_path / "f.py").read_text() == "X = 1\n"
    assert reverted.data["reverted"] == feature_sha


def test_write_outside_approved_root_raises(tmp_path) -> None:
    _init_repo(tmp_path)
    broker = ToolBroker.from_store(_store(tmp_path), repo_root=tmp_path)
    task = Task(title="escape")
    outside = tmp_path.parent / "escape.py"

    with pytest.raises(PermissionError):
        broker.request(
            CapabilityRequest(
                agent="Builder",
                tool="repo.write_file",
                task_id=task.id,
                args={"path": str(outside), "content": "x"},
            ),
            task,
        )
