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

- [x] **Document the council contract.** [docs/council-contract.md](docs/council-contract.md)
  is the operator-facing spec: every lens (Intent, Optimise, Simple, Efficiency,
  Sentinel) and the LLM cognition layer, what each checks, what `pass` / `escalate`
  / `block` mean, the veto-not-vote aggregation, and the full broker pipeline
  mapping each outcome to deny / park / notify / pass under review-after.

## Rollback and External Actions

- [ ] **Define rollback beyond git.** The current rollback story is strong for
  code actions that record a commit sha, but future communications and physical
  actions have no inverse commit. Keep fail-closed manual undo, but design
  per-tool compensating actions, audit requirements, and operator prompts before
  adding non-git mutating surfaces.

## Budgeting

- [x] **Replace estimated foundation spend with measured usage.** Foundation
  spend is now recorded from real API token usage: `llm.record_llm_usage` (called
  in every OpenAI-compatible `_complete`, the central seam all rotating brain
  instances share) prices `usage` against `FOUNDATION_PRICING_USD_PER_MTOK`;
  `drain_foundation_spend_usd` is drained by the daemon/UI/scrum tick in place of
  the flat `$0.05`. Browser foundation never hits the API path, so it accrues
  nothing (free); local models are unpriced and accrue nothing. `can_escalate`'s
  daily-cap gate now reads measured spend. The estimate key is legacy.

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
