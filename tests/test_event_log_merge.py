from __future__ import annotations

import pytest

from orac.event_log_merge import BoardMergeConflict, merge_boards
from orac.models import Board, Task, TaskStatus
from orac.storage import BoardStore, StaleBoardError


# --- unit: the 3-way merge rules --------------------------------------------

def _board(*tasks: dict) -> dict:
    return {"created_at": "t0", "updated_at": "t0", "revision": 1, "tasks": list(tasks)}


def _t(tid: str, **fields) -> dict:
    base = {"id": tid, "title": tid, "status": "ready"}
    base.update(fields)
    return base


def test_merge_keeps_non_conflicting_changes_from_both_sides() -> None:
    ancestor = _board(_t("x"))
    theirs = _board(_t("x"), _t("b"))           # concurrent writer added b
    ours = _board(_t("x"), _t("a"))             # we added a
    merged = merge_boards(ancestor, theirs, ours)
    ids = {t["id"] for t in merged["tasks"]}
    assert ids == {"x", "a", "b"}
    assert "revision" not in merged                # storage assigns it


def test_merge_takes_the_side_that_changed_a_task() -> None:
    ancestor = _board(_t("x", status="ready"))
    theirs = _board(_t("x", status="done"))        # they advanced x; we left it
    ours = _board(_t("x", status="ready"))
    merged = merge_boards(ancestor, theirs, ours)
    assert merged["tasks"][0]["status"] == "done"


def test_merge_preserves_a_one_sided_deletion() -> None:
    ancestor = _board(_t("x"), _t("y"))
    theirs = _board(_t("x"), _t("y"))              # they didn't touch y
    ours = _board(_t("x"))                          # we removed y
    merged = merge_boards(ancestor, theirs, ours)
    assert {t["id"] for t in merged["tasks"]} == {"x"}


def test_merge_raises_on_a_true_conflict() -> None:
    ancestor = _board(_t("x", status="ready"))
    theirs = _board(_t("x", status="done"))
    ours = _board(_t("x", status="blocked"))        # both changed x, differently
    with pytest.raises(BoardMergeConflict) as exc:
        merge_boards(ancestor, theirs, ours)
    assert "x" in exc.value.task_ids


def test_merge_remove_vs_edit_is_a_conflict() -> None:
    ancestor = _board(_t("x", status="ready"))
    theirs = _board(_t("x", status="done"))         # they edited x
    ours = _board()                                  # we removed x
    with pytest.raises(BoardMergeConflict):
        merge_boards(ancestor, theirs, ours)


# --- integration: concurrent BoardStore writers -----------------------------

def test_concurrent_writers_merge_transparently(tmp_path) -> None:
    store = BoardStore(tmp_path)
    store.init()                                     # revision 1 on disk

    a = store.load()
    b = store.load()
    assert a.revision == b.revision == 1

    b.add_task(Task(title="from B"))
    store.save(b)                                    # revision 2

    # A is now stale, but its change is independent of B's — it must merge, not fail.
    a.add_task(Task(title="from A"))
    store.save(a)

    final = store.load()
    titles = {t.title for t in final.tasks}
    assert {"from A", "from B"} <= titles
    assert final.revision == 3


def test_concurrent_edit_of_the_same_task_raises(tmp_path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    shared = Task(title="shared", status=TaskStatus.READY)
    board.add_task(shared)
    store.save(board)

    a = store.load()
    b = store.load()
    b.get_task(shared.id).transition(TaskStatus.DONE)
    store.save(b)

    a.get_task(shared.id).transition(TaskStatus.BLOCKED)
    with pytest.raises(StaleBoardError):
        store.save(a)


def test_one_sided_delete_merges_against_unrelated_concurrent_add(tmp_path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    doomed = Task(title="doomed")
    board.add_task(doomed)
    store.save(board)

    a = store.load()
    b = store.load()
    b.add_task(Task(title="B extra"))                # B touches a different task
    store.save(b)

    a.tasks = [t for t in a.tasks if t.id != doomed.id]  # A removes the doomed one
    store.save(a)

    final = store.load()
    ids = {t.id for t in final.tasks}
    assert doomed.id not in ids                      # A's deletion preserved
    assert any(t.title == "B extra" for t in final.tasks)  # B's add preserved
