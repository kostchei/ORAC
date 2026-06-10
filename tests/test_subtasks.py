from __future__ import annotations

import subprocess

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.llm import RulesBrain
from orac.models import Board, Task, TaskStatus
from orac.scrum import Scrum
from orac.subtasks import (
    FileWrite,
    SubtaskContract,
    execute_build_subtask,
    run_build,
    spawn_build_subtask,
)


def _git(path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _setup(tmp_path):
    (tmp_path / ".orac").mkdir()
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-m", "init")
    store = BrokerStore(tmp_path).init()
    return ToolBroker.from_store(store, repo_root=tmp_path), store


def _contract(tmp_path, *, passing: bool = True) -> SubtaskContract:
    body = "def add(a, b):\n    return a + b\n"
    expected = "3" if passing else "4"
    test = (
        "from mod import add\n\n"
        f"def test_add():\n    assert add(1, 2) == {expected}\n"
    )
    return SubtaskContract(
        goal="add a tiny module",
        file_writes=(
            FileWrite(path=str(tmp_path / "mod.py"), content=body),
            FileWrite(path=str(tmp_path / "test_mod.py"), content=test),
        ),
        acceptance_criteria=("add() works",),
        test_target=str(tmp_path / "test_mod.py"),
    )


def test_spawn_creates_child_with_contract_and_parent_link(tmp_path) -> None:
    board = Board()
    parent = Task(title="improve the system")
    board.add_task(parent)

    child = spawn_build_subtask(board, parent, _contract(tmp_path))

    assert child.parent_id == parent.id
    assert child.assignee == "Builder"
    assert child.status == TaskStatus.READY
    assert child in board.tasks
    roundtrip = SubtaskContract.from_dict(child.metadata["contract"])
    assert roundtrip.goal == "add a tiny module"
    assert len(roundtrip.file_writes) == 2
    assert any("Spawned build subtask" in log.message for log in parent.work_log)


def test_parent_id_survives_board_serialization(tmp_path) -> None:
    board = Board()
    parent = Task(title="parent")
    board.add_task(parent)
    child = spawn_build_subtask(board, parent, _contract(tmp_path))

    restored = Board.from_dict(board.to_dict())

    assert restored.get_task(child.id).parent_id == parent.id
    assert restored.get_task(child.id).metadata["contract"]["goal"] == "add a tiny module"


def test_run_build_happy_path_rolls_summary_up(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    board = Board()
    parent = Task(title="improve the system", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    child = run_build(board, parent, _contract(tmp_path), broker, str(tmp_path))

    assert child.status == TaskStatus.DONE
    assert parent.status == TaskStatus.IN_PROGRESS  # parent not disturbed
    rollup = parent.work_log[-1].message
    assert "done" in rollup and "tests passed" in rollup

    # the work is real: branch exists, commit contains exactly the two files
    branches = subprocess.run(
        ["git", "branch", "--list", f"build/{child.id}"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert f"build/{child.id}" in branches.stdout
    shown = subprocess.run(
        ["git", "show", "--stat", "--name-only", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert "mod.py" in shown.stdout and "test_mod.py" in shown.stdout

    # and the whole run is audited as Builder activity
    audited = {(e.agent, e.tool) for e in store.audit_log()}
    assert ("Builder", "git.create_branch") in audited
    assert ("Builder", "repo.run_tests") in audited


def test_run_build_failing_tests_blocks_child_and_parent(tmp_path) -> None:
    broker, _ = _setup(tmp_path)
    board = Board()
    parent = Task(title="improve the system", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    child = run_build(board, parent, _contract(tmp_path, passing=False), broker, str(tmp_path))

    assert child.status == TaskStatus.BLOCKED
    assert parent.status == TaskStatus.BLOCKED
    assert "FAILED" in parent.work_log[-1].message


def test_council_loop_skips_builder_subtasks(tmp_path) -> None:
    board = Board()
    parent = Task(title="parent")
    board.add_task(parent)
    child = spawn_build_subtask(board, parent, _contract(tmp_path))
    logs_before = len(child.work_log)

    Scrum(RulesBrain()).run(board, cycles=2)

    # the council never touched the child; it stays READY for its runner
    assert child.status == TaskStatus.READY
    assert len(child.work_log) == logs_before
