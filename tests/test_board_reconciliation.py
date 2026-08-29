from __future__ import annotations

import json
from pathlib import Path
import pytest

from orac.models import Board, Task, TaskStatus
from orac.self_tune import goal_outcomes
from orac.storage import BoardStore


def test_prune_tasks_archive(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()

    t1 = Task(title="Active Task 1", status=TaskStatus.IN_PROGRESS, work_kind="code", metadata={"goal": "g1"})
    t2 = Task(title="Legacy Blocked 1", status=TaskStatus.BLOCKED, work_kind="code", metadata={"goal": "g2"})
    t3 = Task(title="Legacy Blocked 2", status=TaskStatus.BLOCKED, work_kind="code", metadata={"goal": "g3"})
    t4 = Task(title="Done Task 1", status=TaskStatus.DONE, work_kind="code", metadata={"goal": "g4"})

    for t in (t1, t2, t3, t4):
        board.add_task(t)
    store.save(board)

    done, blocked = goal_outcomes(board)
    assert done == 1
    assert blocked == 2

    # Prune blocked tasks
    pruned = store.prune_tasks(lambda task: task.status == TaskStatus.BLOCKED, archive=True)
    assert len(pruned) == 2

    reloaded = store.load()
    assert len(reloaded.tasks) == 2
    assert {t.title for t in reloaded.tasks} == {"Active Task 1", "Done Task 1"}

    # goal_outcomes reflects clean board
    done_after, blocked_after = goal_outcomes(reloaded)
    assert done_after == 1
    assert blocked_after == 0

    # Check archive file
    assert store.archive_path.exists()
    archived = json.loads(store.archive_path.read_text(encoding="utf-8"))
    assert len(archived) == 2
    assert {t["id"] for t in archived} == {t2.id, t3.id}


def test_prune_tasks_mark_superseded(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()

    t1 = Task(title="Legacy Blocked", status=TaskStatus.BLOCKED, work_kind="code", metadata={"goal": "g1"})
    t2 = Task(title="Done Task", status=TaskStatus.DONE, work_kind="code", metadata={"goal": "g2"})
    board.add_task(t1)
    board.add_task(t2)
    store.save(board)

    # Mark as superseded in place
    pruned = store.prune_tasks(lambda task: task.status == TaskStatus.BLOCKED, mark_superseded=True)
    assert len(pruned) == 1

    reloaded = store.load()
    assert len(reloaded.tasks) == 2
    superseded_task = [t for t in reloaded.tasks if t.id == t1.id][0]
    assert superseded_task.metadata.get("superseded") is True

    # self_tune ignores superseded tasks
    done, blocked = goal_outcomes(reloaded)
    assert done == 1
    assert blocked == 0
