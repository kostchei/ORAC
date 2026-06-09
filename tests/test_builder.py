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
