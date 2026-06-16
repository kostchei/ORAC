# Board concurrency: StaleBoardError and transparent event-log merge

**Status:** Implemented · **Owner:** ORAC core · **Code:** [storage.py](../src/orac/storage.py),
[event_log_merge.py](../src/orac/event_log_merge.py)

ORAC's board is written by more than one process at once — the daemon tick advancing tasks, the UI
server adding goals or editing settings. This documents how concurrent writes are reconciled.

## The persistence model (what's actually on disk)

- **`board.json`** — the authoritative current snapshot, carrying a monotonic `revision` counter.
- **`board.last-good.json`** — a mirror backup for `orac board recover`.
- **`board.events.jsonl`** — an append-only log; **each line is a full board snapshot** for one
  committed revision, plus a human-readable change summary. Because every event is a complete
  snapshot, rebuild is trivially "take the latest", and any past revision can be fetched whole.
- **`board.lock`** — an OS file lock (msvcrt/fcntl) held across the save critical section; the kernel
  releases it if the holder dies, so there are no stale locks.

## How a conflict arises

`BoardStore.load()` reads `board.json` **without** the lock and returns a `Board` stamped with the
revision it read. `BoardStore.save()` then takes the lock and re-reads the current revision. If
another writer committed in between, the in-memory board's `revision` is behind the file's — a
potential lost update.

## How it is handled: detect → 3-way merge → raise only on true conflict

Inside the lock, on a revision mismatch, `save()` does **not** immediately fail. It attempts a
transparent three-way merge (`_merge_in_place` → `event_log_merge.merge_boards`):

- **ancestor** — the snapshot at our loaded revision, fetched whole from the event log
  (`_event_board_at`). This is the exact common base both writers started from.
- **theirs** — the board now on disk (the concurrent writer's commit).
- **ours** — the in-memory board this save wants to write.

The merge is per task id. `None` (absent) is treated as an ordinary value, so add / remove / edit are
handled uniformly:

| ours vs ancestor | theirs vs ancestor | result |
| --- | --- | --- |
| unchanged | changed (incl. delete) | take **theirs** |
| changed (incl. delete) | unchanged | take **ours** |
| changed the same way | changed the same way | take either (identical) |
| changed differently | changed differently | **conflict** |

A merge that succeeds is written at the current revision + 1, inside the same lock, so the operation
is atomic — no retry loop is needed because no other writer can intervene while the lock is held.

**Raise-on-conflict (the chosen rule).** Two situations still raise `StaleBoardError`, consistent
with ORAC's "no fallbacks — fail loudly" stance:

1. A single task was edited differently on both sides (a genuine conflict — e.g. the daemon marked it
   `done` while the UI moved it to `blocked`). `BoardMergeConflict` is raised inside the merge and
   surfaced as `StaleBoardError`.
2. The common ancestor cannot be found in the event log (a truncated log, or a board that was never
   loaded from disk — e.g. a blind `save(Board())`, whose revision 0 has no event). Without a base
   there is no safe merge, so it fails closed.

The caller handles `StaleBoardError` by reloading and reapplying — the same contract as before; what
changed is that the common case (independent edits) now merges silently instead of forcing that
reload.

## What is intentionally *not* done

- **No automatic per-field merge of a conflicting task.** A task edited on both sides raises rather
  than guessing which field wins. (Last-writer-wins by timestamp was considered and rejected in
  favour of failing loudly.)
- **No background retry/backoff.** Reconciliation happens once, synchronously, under the lock.
