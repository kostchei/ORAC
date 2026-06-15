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

## Conflict resolution rule: reapply, don't merge
The loser of the revision CAS does **not** try to three-way-merge task fields.
It reloads the current board and **reapplies its own mutation** to the fresh
state, then saves again:

- **UI writer** — the operator action is a single, well-scoped change (ack a
  notification, edit one task). On `StaleBoardError`, reload and reapply that one
  action. These are short and almost always win the retry.
- **Daemon writer** — a tick is large and derived from board state. The cheapest
  correct response to `StaleBoardError` is to **discard the recomputed board and
  re-run the tick** against the freshly-loaded board, rather than reapplying a
  stale diff. Tick work is idempotent at the granularity of a board load, so a
  re-run is safe.

This keeps a single source of truth (`board.json`) and makes every lost-update a
loud, recoverable `StaleBoardError` rather than silent corruption.

## What is NOT done, and why
- **No per-field/CRDT merge.** Tasks have interdependent fields (status, ledger,
  work log); a blind field merge could produce a board no single writer intended.
  Reapply-on-fresh-state is simpler and always yields an intentional board.
- **No timestamp-priority or "daemon-override" tie-break.** The lock + revision
  CAS already gives a total order; there are no ties to break.

## Open items
- Wire the **reapply loop** explicitly at both call sites (daemon `run_daemon_tick`
  and the UI server's write path): catch `StaleBoardError`, reload, reapply/re-run,
  bounded retry. Today the error is raised correctly but callers do not yet all
  retry — that is the remaining work this design covers.
- Consider shrinking the daemon's lock-held window if UI writes start starving
  (currently a full tick can hold the board across its compute; a tick that only
  takes the lock for the final save would reduce contention).

## Source of truth
`storage.py`: `BoardStore.save`, `_BoardLock`, `StaleBoardError`, `_save_atomic`,
`_append_event`, `read_events`, `rebuild_from_events`, `restore_from_events`.
