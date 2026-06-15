# Board Write Conflicts: Daemon and UI Writers

## The problem
Two processes write the board: the **daemon** (long `Scrum.run` ticks) and the
**UI server** (small, frequent edits from operator actions). Both follow
load → mutate → save. Without coordination the second save silently overwrites
the first writer's changes — a lost update.

## What ORAC actually does — there is no field-level "merge"
The name "event-log merge" is a misnomer. Conflicts are resolved by **optimistic
concurrency control + reapply**, and the event log is an **append-only snapshot
log**, not a set of deltas that get merged. The mechanism lives in
`storage.py::BoardStore` and is already implemented:

1. **Exclusive lock around the critical section.** `save()` takes `_BoardLock`
   (an OS file lock via `msvcrt`/`fcntl` on `board.lock`). Only one writer is in
   the check-and-swap at a time, across processes. The kernel drops the lock if a
   holder dies, so a crash cannot wedge the board.

2. **Revision compare-and-swap.** Each board carries a monotonic `revision`.
   Inside the lock, `save()` re-reads the on-disk revision; if it differs from the
   in-memory board's revision, another writer committed in between and `save()`
   raises **`StaleBoardError`** instead of overwriting. The winning write bumps
   `revision = current + 1`; the loser must **reload and reapply**.

3. **Atomic file swap.** `_save_atomic` writes to a temp file, `fsync`s, then
   `os.replace`s — so a reader never sees a torn `board.json`. The same payload is
   mirrored to `board.last-good.json`.

4. **Append-only event log.** Still inside the lock and only after `board.json` is
   durable, `_append_event` appends one line to `board.events.jsonl`: a full board
   **snapshot** plus a human-readable change summary, with `seq == revision`.
   Because every line is a complete snapshot, rebuild is trivial — take the line
   with the highest `revision` (`rebuild_from_events` / `restore_from_events`).
   There is no replay-order or merge-inequality risk, and the log can never get
   ahead of the authoritative board.

## Conflict resolution — IMPLEMENTED
The loser of the revision CAS recovers instead of raising. Two strategies, chosen
by whether the writer's mutation can be safely re-run:

- **Pure writers → reapply** (`BoardStore.update(mutate)`). A chat goal-add, a UI
  base-request, a settings change — each is a *pure function* of the board. On
  `StaleBoardError`, reload the current board, reapply the mutation, save again
  (bounded retries). Reapply against fresh state is exact, so the writer always
  lands without clobbering the concurrent update. Wired at: chat `_add_goal`,
  UI `/api/requests`.
- **The daemon tick → task-level three-way merge** (`BoardStore.save_merging(board,
  base)` + `board_merge.merge_boards`). A tick runs a long, **side-effecting**
  Scrum cycle (git commits, subagent spawns) — re-running it would double-execute,
  so reapply is *not* safe here. Instead, on conflict we reload the current board
  and three-way merge the tick's board against it, using the loaded board as the
  common ancestor (`base`; recoverable from the event log at that revision). Tasks
  are the merge unit, keyed by `id`: the daemon's in-flight tasks and a concurrent
  writer's new task are disjoint and union cleanly. Wired at: daemon
  `run_daemon_tick`, UI `UIRuntime._loop`, UI `/api/run`.

This keeps a single source of truth (`board.json`) and turns every lost-update
into a transparent recovery instead of a dropped tick.

## Merge semantics (`board_merge.merge_boards(base, ours, theirs)`)
Per task id, against the common `base`:
- changed by only one side → take that side's version;
- added by one side (absent from base) → keep it;
- deleted by one side, untouched by the other → honor the deletion;
- **conflict** (both changed the same task differently, or modify-vs-delete) →
  resolve by newest `updated_at` and **report** the id (never silently dropped).
Field-level/CRDT merge is deliberately avoided — a task's fields (status, ledger,
work log) are interdependent, so the merge unit is the whole task.

## Open items
- **Surface reported conflicts.** `save_merging` returns the conflicting task ids;
  callers currently discard them. Log/notify on non-empty conflicts so a genuine
  same-task race is operator-visible (rare: the writers touch disjoint tasks).
- Consider shrinking the daemon's lock-held window if UI writes ever starve
  (today the lock is only held for the final atomic save, not across the tick's
  compute — so contention is already low; revisit only if observed).

## Source of truth
`storage.py`: `BoardStore.save`, `update`, `save_merging`, `_board_at_revision`,
`_BoardLock`, `StaleBoardError`, `_save_atomic`, `_append_event`, `read_events`,
`rebuild_from_events`, `restore_from_events`. `board_merge.py`: `merge_boards`,
`BoardMerge`.
