from __future__ import annotations

import copy

from orac.board_merge import merge_boards
from orac.models import Board, Task, TaskStatus


def _board(tasks: list[Task], *, revision: int = 5) -> Board:
    return Board(tasks=tasks, revision=revision)


def _task(tid: str, *, status: TaskStatus = TaskStatus.READY, updated_at: str = "2026-01-01T00:00:00+00:00", title: str | None = None) -> Task:
    return Task(id=tid, title=title or f"task {tid}", status=status, updated_at=updated_at)


def test_disjoint_changes_union_cleanly() -> None:
    # base: one task A. ours advances A; theirs adds a new task B.
    base = _board([_task("A", status=TaskStatus.READY)])
    ours = copy.deepcopy(base)
    ours.tasks[0].status = TaskStatus.DONE
    theirs = copy.deepcopy(base)
    theirs.tasks.append(_task("B"))
    theirs.revision = 7

    merged = merge_boards(base, ours, theirs)

    by_id = {t.id: t for t in merged.board.tasks}
    assert set(by_id) == {"A", "B"}
    assert by_id["A"].status is TaskStatus.DONE   # ours' change kept
    assert by_id["B"].id == "B"                   # theirs' new task kept
    assert merged.conflicts == []
    assert merged.board.revision == 7             # supersedes current on-disk


def test_untouched_by_us_takes_their_version() -> None:
    base = _board([_task("A", status=TaskStatus.READY)])
    ours = copy.deepcopy(base)                     # we did not touch A
    theirs = copy.deepcopy(base)
    theirs.tasks[0].status = TaskStatus.BLOCKED    # they changed A

    merged = merge_boards(base, ours, theirs)

    assert merged.board.tasks[0].status is TaskStatus.BLOCKED
    assert merged.conflicts == []


def test_same_task_conflict_resolves_to_newest_update() -> None:
    base = _board([_task("A", status=TaskStatus.READY, updated_at="2026-01-01T00:00:00+00:00")])
    ours = copy.deepcopy(base)
    ours.tasks[0].status = TaskStatus.DONE
    ours.tasks[0].updated_at = "2026-01-01T00:00:05+00:00"
    theirs = copy.deepcopy(base)
    theirs.tasks[0].status = TaskStatus.BLOCKED
    theirs.tasks[0].updated_at = "2026-01-01T00:00:09+00:00"   # newer

    merged = merge_boards(base, ours, theirs)

    assert merged.conflicts == ["A"]
    assert merged.board.tasks[0].status is TaskStatus.BLOCKED  # newer wins


def test_deletion_by_one_side_is_honored_when_other_untouched() -> None:
    base = _board([_task("A"), _task("B")])
    ours = copy.deepcopy(base)                     # we keep both, untouched
    theirs = copy.deepcopy(base)
    theirs.tasks = [t for t in theirs.tasks if t.id != "B"]  # they deleted B

    merged = merge_boards(base, ours, theirs)

    assert {t.id for t in merged.board.tasks} == {"A"}
    assert merged.conflicts == []


def test_modify_delete_race_keeps_the_modification_and_flags_conflict() -> None:
    base = _board([_task("A"), _task("B", status=TaskStatus.READY)])
    ours = copy.deepcopy(base)
    ours.tasks[1].status = TaskStatus.DONE         # we modified B
    theirs = copy.deepcopy(base)
    theirs.tasks = [t for t in theirs.tasks if t.id != "B"]  # they deleted B

    merged = merge_boards(base, ours, theirs)

    by_id = {t.id: t for t in merged.board.tasks}
    assert by_id["B"].status is TaskStatus.DONE    # modification preserved
    assert merged.conflicts == ["B"]


def test_new_tasks_from_both_sides_are_all_kept() -> None:
    base = _board([_task("A")])
    ours = copy.deepcopy(base)
    ours.tasks.append(_task("O"))                  # daemon spawned a subtask
    theirs = copy.deepcopy(base)
    theirs.tasks.append(_task("T"))                # chat added a goal

    merged = merge_boards(base, ours, theirs)

    assert {t.id for t in merged.board.tasks} == {"A", "O", "T"}
    assert merged.conflicts == []
