"""Three-way, task-keyed merge of two divergent board states.

When two writers (the daemon tick and a UI/chat writer) both load the board at
revision N and then save, the second save raises ``StaleBoardError`` — it would
otherwise destroy the first writer's update. The recovery is a merge: reconcile
*our* board against the *current* on-disk board using their common *base*
(revision N's snapshot, which the append-only event log preserves).

Tasks are the merge unit, keyed by their stable ``id``. The two writers almost
always touch *disjoint* tasks — the daemon advances its in-flight tasks while a
chat/UI writer adds a new one — so the merge is a clean union. A genuine
conflict (both sides changed the *same* task differently) is resolved by the
newest ``updated_at`` and reported, so it is never silently dropped.
"""
from __future__ import annotations

from dataclasses import dataclass

from orac.models import Board, Task


@dataclass(frozen=True)
class BoardMerge:
    board: Board
    # Task ids where BOTH sides changed the same task differently (or one side
    # modified a task the other deleted). Resolved, not dropped — surfaced so the
    # caller can log them.
    conflicts: list[str]


def _changed(task: Task, task_id: str, base: dict[str, dict]) -> bool:
    """Did this side add or modify the task relative to the common base?"""
    return task_id not in base or task.to_dict() != base[task_id]


def merge_boards(base: Board, ours: Board, theirs: Board) -> BoardMerge:
    """Merge ``ours`` onto ``theirs`` using their common ancestor ``base``.

    Returns a board whose ``revision`` is ``theirs.revision`` — i.e. it supersedes
    the current on-disk board — so the caller can hand it straight to ``save``.
    """
    base_by_id = {task.id: task.to_dict() for task in base.tasks}
    ours_by_id = {task.id: task for task in ours.tasks}
    theirs_by_id = {task.id: task for task in theirs.tasks}

    decided: dict[str, Task | None] = {}
    conflicts: list[str] = []

    for task_id in set(ours_by_id) | set(theirs_by_id):
        ours_task = ours_by_id.get(task_id)
        theirs_task = theirs_by_id.get(task_id)
        in_base = task_id in base_by_id

        if ours_task is not None and theirs_task is not None:
            ours_changed = _changed(ours_task, task_id, base_by_id)
            theirs_changed = _changed(theirs_task, task_id, base_by_id)
            if (
                ours_changed
                and theirs_changed
                and ours_task.to_dict() != theirs_task.to_dict()
            ):
                conflicts.append(task_id)
                decided[task_id] = (
                    ours_task
                    if ours_task.updated_at >= theirs_task.updated_at
                    else theirs_task
                )
            elif ours_changed:
                decided[task_id] = ours_task
            else:
                # theirs changed it, or neither did — take the on-disk version.
                decided[task_id] = theirs_task
        elif ours_task is not None:
            # Present only in ours: either we added it, or they deleted it.
            if not in_base:
                decided[task_id] = ours_task  # we added it
            elif _changed(ours_task, task_id, base_by_id):
                # modify/delete race: keep the modification rather than lose work.
                conflicts.append(task_id)
                decided[task_id] = ours_task
            else:
                decided[task_id] = None  # they deleted it, we did not touch it
        else:
            # Present only in theirs: either they added it, or we deleted it.
            if not in_base:
                decided[task_id] = theirs_task  # they added it
            elif _changed(theirs_task, task_id, base_by_id):
                conflicts.append(task_id)
                decided[task_id] = theirs_task
            else:
                decided[task_id] = None  # we deleted it, they did not touch it

    # Stable order: keep the on-disk (theirs) order, then append our new tasks.
    order: list[str] = []
    seen: set[str] = set()
    for task in [*theirs.tasks, *ours.tasks]:
        if task.id not in seen:
            seen.add(task.id)
            order.append(task.id)
    merged_tasks = [decided[tid] for tid in order if decided.get(tid) is not None]

    merged = Board(
        tasks=merged_tasks,  # type: ignore[arg-type]  # None filtered above
        created_at=base.created_at,
        updated_at=max(ours.updated_at, theirs.updated_at),
        revision=theirs.revision,
    )
    return BoardMerge(board=merged, conflicts=conflicts)
