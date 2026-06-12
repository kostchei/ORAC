# ORAC TODO

High-signal follow-up items from the current appraisal. These are not feature
ideas; they are verification, safety, and durability gaps that should be closed
before ORAC widens beyond the code-writing bootstrap.

## Safety and Verification

- [x] **Prove the governance path, not just the docs.**
  `scripts/validate_governance_path.py` now runs a cross-cutting smoke suite
  against real broker dispatch calls. It confirms clean allowed dispatch,
  Intent block, Efficiency duplicate-write block, Optimise fair-share
  escalation, Sentinel safety-critical escalation before dispatch,
  review-after `git.push` notification, and standing-grant daily-cap fallback
  to pending approval. Covered by `tests/test_governance_validation_script.py`.

- [ ] **Document the council contract.** Give the council, Sentinel, fair-share
  band, churn lens, duplicate-write lens, and verdict aggregation their own
  operator-facing contract: what each lens checks, what `pass` / `escalate` /
  `block` mean, and which outcomes park, deny, notify, or pass.

## Rollback and External Actions

- [ ] **Define rollback beyond git.** The current rollback story is strong for
  code actions that record a commit sha, but future communications and physical
  actions have no inverse commit. Keep fail-closed manual undo, but design
  per-tool compensating actions, audit requirements, and operator prompts before
  adding non-git mutating surfaces.

## Budgeting

- [ ] **Replace estimated foundation spend with measured usage.** The current
  `$0.05` per productive cycle estimate is a placeholder. Record actual API
  token/cost usage from response metadata where available, keep browser-provider
  usage separate, and make routing decisions from measured spend instead of the
  estimate when possible.

## Optional Surfaces

- [ ] **Quarantine audio from the core loop.** Treat browser mic permission,
  WebM, `ffmpeg`, Whisper, and local TTS as convenience features only. Audio
  failures must never block task flow, daemon ticks, review handling, or the
  Builder path.

## State Durability

- [x] **Harden board state (minimum bar).** All JSON state writes
  (`board.json`, `config.json`, `usage.json`) now go through
  write-temp-fsync-then-rename (`BoardStore._save_atomic`): a daemon death or
  power loss mid-write leaves the previous file intact, and failed saves clean
  up their temp file. A corrupt board fails closed (`CorruptStateError`) and
  `orac board recover` restores the `board.last-good.json` backup refreshed on
  every save. Concurrent writers are guarded by an OS file lock plus a board
  `revision` check: a save based on a stale revision raises `StaleBoardError`
  instead of silently destroying the other writer's updates (the daemon tick
  vs. UI server window). Covered by `tests/test_storage.py`.

- [ ] **Resolve write conflicts, don't just detect them.** `StaleBoardError`
  turns the daemon-tick vs. UI-server lost-update race from silent data loss
  into a loud failure, but the losing writer still has no way to merge its
  changes. Proper resolution is the event log's job (below).

- [ ] **Board event log.** The preferred end-state remains an append-only
  event log that can rebuild the board and unify with the audit trail; the
  atomic-write hardening above is the stopgap, not the destination.
