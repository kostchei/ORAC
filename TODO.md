# ORAC TODO

High-signal follow-up items from the current appraisal. These are not feature
ideas; they are verification, safety, and durability gaps that should be closed
before ORAC widens beyond the code-writing bootstrap.

## Safety and Verification

- [ ] **Prove the governance path, not just the docs.** Add an explicit
  verification checklist or smoke suite that confirms the council lenses,
  Sentinel gate, fair-share band, standing-grant daily caps, and pending/notify
  outcomes are actually wired through the dispatch path. These are load-bearing
  controls; if any are only partially wired, the review-after posture becomes
  risk-accepting rather than risk-managed.

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

- [ ] **Harden board state.** The board is ORAC's memory. Confirm atomic writes,
  corruption recovery, and daemon-death behavior mid-tick. At minimum use
  write-temp-then-rename for JSON state; preferably move toward an append-only
  event log that can rebuild the board and unify with the audit trail.
