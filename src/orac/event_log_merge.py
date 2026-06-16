from __future__ import annotations

from typing import Any

from orac.models import now_iso

# Transparent board merge for concurrent writers (the daemon advancing tasks while
# the UI adds goals / edits settings). The board event log already stores a full
# snapshot per revision, so when a save hits a revision mismatch we have the exact
# common ancestor (the loaded revision's snapshot) and can do a real 3-way merge by
# task id rather than failing the write.
#
# Merge rule (the project's "raise on conflict" decision): non-conflicting changes
# from both sides are always preserved; a single task changed differently on both
# sides — or removed on one side and edited on the other — is a true conflict and
# raises BoardMergeConflict (which storage surfaces as StaleBoardError). No silent
# last-writer-win: a genuine conflict stops loudly.


class BoardMergeConflict(RuntimeError):
    """A task was edited by both the concurrent writer and this save in ways that
    cannot be reconciled automatically."""

    def __init__(self, task_ids: list[str]) -> None:
        self.task_ids = list(task_ids)
        super().__init__(
            "Board merge conflict on task(s) "
            + ", ".join(self.task_ids)
            + ": edited by both the concurrent writer and this save."
        )


def _by_id(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in board.get("tasks", []) if "id" in t}


def merge_boards(
    ancestor: dict[str, Any], theirs: dict[str, Any], ours: dict[str, Any]
) -> dict[str, Any]:
    """Three-way merge of board snapshots by task id.

    - ``ancestor`` — the state both writers started from (our loaded revision).
    - ``theirs``   — the state now on disk (a concurrent writer committed it).
    - ``ours``     — the in-memory board this save wants to write.

    Returns a merged board dict (``revision`` omitted — the caller assigns it).
    Raises :class:`BoardMergeConflict` when a task changed incompatibly on both
    sides. ``None`` for a task id means "absent" (added/removed), and is compared
    like any other value, so add/remove/edit are all handled uniformly: a side
    that matches the ancestor for a task did not touch it, so the other side's
    version (including a deletion) wins.
    """
    a, t, o = _by_id(ancestor), _by_id(theirs), _by_id(ours)

    # Preserve disk (theirs) ordering, then append ids only we have.
    ordered_ids = list(t)
    ordered_ids += [i for i in o if i not in t]

    merged: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for i in ordered_ids:
        av, tv, ov = a.get(i), t.get(i), o.get(i)
        if ov == tv:                      # both sides agree (incl. both absent)
            if ov is not None:
                merged.append(ov)
        elif ov == av:                    # we didn't touch it -> take theirs
            if tv is not None:
                merged.append(tv)
        elif tv == av:                    # they didn't touch it -> take ours
            if ov is not None:
                merged.append(ov)
        else:                             # both changed it, differently
            conflicts.append(i)

    if conflicts:
        raise BoardMergeConflict(conflicts)

    out = dict(theirs)                    # inherit created_at etc. from disk
    out["tasks"] = merged
    out["updated_at"] = now_iso()
    out.pop("revision", None)             # storage assigns the new revision
    return out
