# ORAC TODO

High-signal follow-up items from the current appraisal. These are operational
verification, safety, and durability gaps to close before ORAC widens beyond
the code-writing and communications surfaces already present.

## Immediate Operational Readiness

- [x] **Reconcile persisted board state with implemented work.** The current
  board contains old blocked tasks from before later manual recovery commits,
  plus stale active subagent reservations. Preserve the event-log history, but
  make the current board and its telemetry distinguish superseded work, genuine
  blockers, and stale reservations so the self-tuning loop does not learn from
  obsolete failures.


- [x] **Run a supervised live-model canary before an overnight daemon.** Start
  LM Studio, verify the configured model slots, run `orac lenses eval`, then
  run `python scripts/soak_validate.py 3`. Record whether each tick completes
  verified work without malformed structured replies, step-budget exhaustion,
  leaked roster reservations, or unexpected review-queue entries.
  **Completed 2026-08-28:** all three configured slots were loadable and the live
  lens scorecard scored 16/16 decisive cases with a healthy 1-pass/2-stop
  borderline split. The first soak exposed structured fallback, search/read
  churn, permissive decision schemas, and a false review-queue remediation loop;
  each was fixed with regression coverage. The final clean run seeded an explicit
  no-edit verification task: parent `e4a2381d` and independently verified child
  `c27fd722` reached `done` in tick 1 (100.8s); ticks 2–3 were idle. It introduced
  no queue entries, pending approvals, malformed/budget logs, dirty files, or
  leaked reservations. HEAD remained `a510ba3`; only the expected checkpoint
  branch changed.

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

- [x] **Define rollback beyond git.** The current rollback story is strong for
  code actions that record a commit sha, but future communications and physical
  actions have no inverse commit. [docs/compensating-actions.md](docs/compensating-actions.md)
  defines the versioned per-tool contract, stale-state and expiry checks, audit
  requirements, operator prompts, and the explicit no-inverse posture that keeps
  messages and unsupported physical/financial actions approval-first. The
  resolver will land with the first Media adapter so the contract is exercised
  by a real non-git tool rather than unused framework code.

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

- [x] **Quarantine audio from the core loop.** Treat browser mic permission,
  WebM, `ffmpeg`, Whisper, and local TTS as convenience features only. Audio
  failures must never block task flow, daemon ticks, review handling, or the
  Builder path. Core UI state now uses hardware-free capability checks; device
  enumeration runs only on the explicit audio endpoint. Device/module failures,
  malformed base64, oversized payloads, and invalid Whisper output return
  bounded error objects. Covered by `tests/test_audio_quarantine.py`.

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

- [x] **Resolve write conflicts, not just detect them.** Pure writers retry by
  reapplying their mutation through `BoardStore.update`; long-running daemon
  work uses `BoardStore.save_merging` and a task-level three-way merge against
  the event-log snapshot. The remaining follow-up is to surface a same-task
  merge conflict to the operator rather than silently retaining the newest task
  version.

- [x] **Board event log.** `BoardStore` now keeps an append-only
  `board.events.jsonl`: every committed board state is appended (full snapshot +
  a human-readable added/updated/removed change summary) inside the same locked
  critical section as the save, fsync'd per line. `read_events` is tolerant of a
  torn final line (a crash mid-append); `rebuild_from_events` / `restore_from_events`
  reconstruct the board from the log's latest snapshot — recovery stronger than the
  single last-good backup (full history, any revision), proven by a test that
  rebuilds after BOTH board.json and the backup are deleted. CLI: `orac board events`
  (history) and `orac board rebuild`. The snapshot-per-commit form makes replay
  trivially correct (no rebuild-inequality risk). The remaining durability
  follow-ups are log compaction/rotation for long runs and a clearer operator
  surface for the rare same-task merge conflict.
