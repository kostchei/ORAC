from __future__ import annotations

import subprocess

import pytest

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.code_adapters import WRITE_TOOLS
from orac.models import CapabilityRequest, CapabilityStatus, Task


def _git(path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _init_repo(path) -> None:
    (path / ".orac").mkdir()
    # mirror the real repo: broker state lives under .orac and is never
    # committed, and the .gitignore itself is committed like any real repo's
    (path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    _git(path, "init", "-b", "main")
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "init")


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

    committed = build(
        "git.commit", message="add hello", paths=[str(tmp_path / "pkg" / "hello.py")]
    )
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
    assert (
        build("git.commit", message="add feature", paths=[str(tmp_path / "f.py")]).status
        is CapabilityStatus.ALLOWED
    )

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

    f_py = str(tmp_path / "f.py")
    (tmp_path / "f.py").write_text("X = 1\n", encoding="utf-8")
    build("git.commit", message="base", paths=[f_py])
    (tmp_path / "f.py").write_text("X = 2\n", encoding="utf-8")
    feature_sha = build("git.commit", message="feature", paths=[f_py]).data["sha"]
    assert (tmp_path / "f.py").read_text() == "X = 2\n"

    # Reviewer says "not ok" -> one-step undo, no history rewrite.
    reverted = build("git.revert", sha=feature_sha)
    assert reverted.status is CapabilityStatus.ALLOWED
    assert (tmp_path / "f.py").read_text() == "X = 1\n"
    assert reverted.data["reverted"] == feature_sha


def test_commit_without_paths_is_refused(tmp_path) -> None:
    _init_repo(tmp_path)
    broker = ToolBroker.from_store(_store(tmp_path), repo_root=tmp_path)
    task = Task(title="sweep")
    (tmp_path / "f.py").write_text("X = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="paths"):
        broker.request(
            CapabilityRequest(
                agent="Builder",
                tool="git.commit",
                task_id=task.id,
                args={"message": "sweep everything"},
            ),
            task,
        )


def test_path_scoped_commit_leaves_other_changes_uncommitted(tmp_path) -> None:
    _init_repo(tmp_path)
    broker = ToolBroker.from_store(_store(tmp_path), repo_root=tmp_path)
    task = Task(title="two changes, one commit")

    def build(tool: str, **args):
        return broker.request(
            CapabilityRequest(agent="Builder", tool=tool, task_id=task.id, args=args), task
        )

    (tmp_path / "feature_a.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "feature_b.py").write_text("B = 1\n", encoding="utf-8")

    build("git.commit", message="feature A only", paths=[str(tmp_path / "feature_a.py")])

    leftover = build("git.status").data["changes"]
    assert any("feature_b.py" in line for line in leftover)
    assert not any("feature_a.py" in line for line in leftover)


def test_individual_feature_revert_leaves_other_feature_intact(tmp_path) -> None:
    _init_repo(tmp_path)
    broker = ToolBroker.from_store(_store(tmp_path), repo_root=tmp_path)
    task = Task(title="fine-grained rollback")

    def build(tool: str, **args):
        return broker.request(
            CapabilityRequest(agent="Builder", tool=tool, task_id=task.id, args=args), task
        )

    (tmp_path / "feature_a.py").write_text("A = 1\n", encoding="utf-8")
    sha_a = build(
        "git.commit", message="feature A", paths=[str(tmp_path / "feature_a.py")]
    ).data["sha"]
    (tmp_path / "feature_b.py").write_text("B = 1\n", encoding="utf-8")
    build("git.commit", message="feature B", paths=[str(tmp_path / "feature_b.py")])

    # Reviewer rejects feature A only.
    build("git.revert", sha=sha_a)

    assert not (tmp_path / "feature_a.py").exists()
    assert (tmp_path / "feature_b.py").read_text() == "B = 1\n"


def test_stash_isolates_unrelated_noise_from_a_commit(tmp_path) -> None:
    _init_repo(tmp_path)
    broker = ToolBroker.from_store(_store(tmp_path), repo_root=tmp_path)
    task = Task(title="stash cycle")

    def build(tool: str, **args):
        return broker.request(
            CapabilityRequest(agent="Builder", tool=tool, task_id=task.id, args=args), task
        )

    (tmp_path / "base.py").write_text("BASE = 0\n", encoding="utf-8")
    build("git.commit", message="base", paths=[str(tmp_path / "base.py")])

    # unrelated half-done noise, then stash it away
    (tmp_path / "noise.py").write_text("WIP = True\n", encoding="utf-8")
    assert build("git.stash", label="half-done noise").data["stashed"] is True
    assert build("git.status").data["changes"] == []

    # clean, focused commit while the noise is shelved
    (tmp_path / "feature.py").write_text("F = 1\n", encoding="utf-8")
    build("git.commit", message="feature", paths=[str(tmp_path / "feature.py")])

    # noise comes back untouched and uncommitted
    build("git.stash_pop")
    assert (tmp_path / "noise.py").read_text() == "WIP = True\n"
    leftover = build("git.status").data["changes"]
    assert any("noise.py" in line for line in leftover)


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
