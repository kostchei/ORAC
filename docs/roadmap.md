# ORAC Roadmap (checkable)

Two orthogonal axes:

- **Foundation** — the governance machinery (broker, council, risk model, Builder).
  See [edge-check-council-design.md](edge-check-council-design.md) §7 for the council subset.
- **Surface** — the capabilities ORAC can exercise, in five categories.
  See [tool-categories.md](tool-categories.md).

**Sequencing principle (unchanged all the way down this project): foundation before breadth.**
The first surface slice — **Code Writing** — is also the *self-improvement bootstrap*: once the
Builder can safely branch/patch/test/commit, ORAC can help build the rest of itself. So Group 1
comes first and groups 2–5 defer behind it. This doc is the master ordering; the design doc's
§7 P-plan is the council-only subset of it.

---

## Done

- [x] Capability contract (`CapabilityRequest` / `CapabilityResult`)
- [x] `ToolBroker` single entry point + enforced per-agent allow-list
- [x] All core agents routed through the broker (`_use`)
- [x] SQLite state: grants, audit, pending_approvals, rate_counters
- [x] `pending_approval` task state + loop park/resume
- [x] First read-only adapter end-to-end (`fs_read`) through the pending path
- [x] Design docs: edge-check council, risk model, Builder role, self-improvement default

---

## Milestone A — Governance spine + Code-Writing bootstrap (NEXT)

Ordered; each step keeps the suite green.

- [x] **P0 — Types.** `EdgeKind`, `LensDecision`, `ReviewContext`, `LensVerdict`, `CouncilVerdict` (+ risk vocabulary `Reversibility`/`Externality`/`RiskClass`) in `models.py`. No behaviour.
- [x] **P1 — Risk throttle.** `policy.py::risk_class(tool, args)` + a total `(reversibility × externality)` throttle table → `auto`/`notify`/`approve`. Broker `_decide` consults it; the `APPROVAL_REQUIRED` stub is gone. Unclassified tools fail closed (raise). **Review-after, not ask-before (user ruling):** code work never blocks — `git.push` is `notify` (runs + lands in the review queue), `git.revert` is the one-step rollback; `approve` is reserved for comms/financial/physical. `notify` is durable now (`notifications` table + ack), transport ping still P6.
- [x] **Builder role + privilege separation.** `builder` agent (kind `doer`, excluded from the council loop) holds the write grants; a test asserts **no reviewer/orchestrator holds any write grant** and that a reviewer write is denied at the broker. (§4.6)
- [x] **Group 1 read slice** (read-only): `repo.read_file`, `repo.search`, `git.status`, `repo.run_tests`.
- [x] **Group 1 write slice** (Builder only, checkpoint-first, confined to approved repo roots): `git.create_branch`, `repo.write_file`, `repo.edit_file`, `git.commit`. *(Substrate: local subprocess git/pytest, no external agent framework. `repo.edit_file` is the surgical primitive — exact, unique, fail-closed string replacement — the deferred `repo.apply_patch` is satisfied in spirit: small reviewable diffs over whole-file rewrites, per the Karpathy guideline. It is wired through every governance layer: Builder-only grant, Sentinel safety gate, LLM lenses, risk classification.)*
- [x] **P2 — Council skeleton.** `council.py`: four deterministic lenses — Intent blocks action on closed tasks (drift), Optimise escalates over the daily rate band (fair share), Simple escalates patch-churn (same tool hammered on one task), Efficiency blocks identical duplicate writes. Convenes on every store-backed call (cheap SQL); per-lens verdicts persisted to a `reviews` table whenever a review is not clean. Rate counters now bumped on every dispatch.
- [x] **P3 — Aggregation + pending.** Any BLOCK → denied (lens reason in the result); any ESCALATE → existing pending/park machinery, cleared by human approval of the exact request. Proven end-to-end: a council ESCALATE parks the task through the agent path, with the `reviews` trail naming the lens that parked it. *(Code stays review-after per user ruling — `git.push` notifies rather than parks.)*
- [x] **P4 — Subtask contract.** `subtasks.py`: `SubtaskContract` (instruction-down, self-contained), `parent_id` child tasks, `run_build` = spawn → Builder executes via broker (branch → path-scoped write/commit → tests) → summary-up to the parent. Tests fail → child+parent BLOCKED. Council loop skips doer subtasks. *Return-edge check is deterministic (tests must pass) until P2/P3 replace it with council review.*
- [x] **Agency.** `agent_session.py`: the loop where the model chooses — fresh context + single
      contract, structured tool decisions, broker adjudicates every choice, denials are
      observations to adapt to, only the summary crosses back. `subtasks.py::run_goal_build`:
      Builder handed a *goal*, not a spec. `driver.py`: initiative — idle board → Optimise reads
      its own telemetry (board, council flags, review queue, roadmap gaps) → originates one
      locked self-improvement task (rate-capped/day; driver faults surface as visible BLOCKED
      tasks). Loop wiring: goal tasks are really built by Builder sessions, never theatrically
      advanced; the daemon originates when idle.
- [x] **General work model.** `work.py::WORK_KINDS` — tasks carry a `work_kind` spanning all five
      categories (code / comms / media / physical / event), each with its sole doer (the §4.6
      one-writer invariant generalised: Messenger will hold `channel.send`, Operator
      `execute_action`, …), its contract rules, and its kind-specific "done means".
      `run_goal_task` is the one runner for every kind; only `code` has a doer today — a goal in
      a doer-less kind blocks visibly, naming the missing capability group.
- [x] **P5 — LLM lenses (live-fire).** The three judgement lenses (Optimise/Simple/Efficiency)
      reason over consequential edges on the resident local model (`lenses.py`), gated to
      state-changing tools (`LLM_REVIEWED_TOOLS`); verdicts aggregate with the deterministic floor
      unchanged. Calibrated and scored against curated cases (`orac lenses eval`), and verified end
      to end on real LM Studio models (reasoning + clean-JSON). *(Moved up from Milestone B: the
      cognition layer landed during the bootstrap rather than after it.)*
- [x] **Review cockpit (the review-after surface).** `orac reviews` shows the queue — pending
      approvals (each annotated with the lens verdict that parked it), completed actions awaiting
      review, and recent lens verdicts (`--json` exports the lot for calibration). `orac approve` /
      `deny` resolve parked requests (the loop resumes/blocks the task on its next tick); `orac ack`
      accepts a completed action; `orac rollback <id> [--push]` git-reverts the recorded commit
      under the `human` audit principal, then acks. `git.push` now records the pushed head sha +
      branch so rollback has a target; a notification with no recorded sha fails closed.
- [ ] **`browser.verify_local_app`** — verification before a task may reach `done` (see Build
      order item 2 below).

**Exit criterion for Milestone A:** idle ORAC picks a self-improvement task, branches, applies a
patch, runs tests, and opens the change for human approval — end-to-end through the council, with
the Builder as the only writer. **Status: the full circle runs in tests** (idle → originate →
locked READY → Builder session builds on a branch with real files and passing tests → DONE →
loop originates the next goal), and the human side of "opens the change for human approval" now
has a real surface (the review cockpit). Caveat: proven with a scripted model; quality with a
live local model (LM Studio tool-format reliability) is the remaining unknown, not the machinery.

---

## Build order (next) — close the loop before widening it

The recommended sequence to reach a daemon you can run unattended overnight. Foundation before
breadth still holds: every item below hardens or completes the governance spine; no new surface
category starts until item 4 has produced real evidence.

1. ~~**Safety-critical-file gate (design §8.7).**~~ **Done.** `policy.SAFETY_CRITICAL_PATHS` +
   `safety_critical_paths_touched(tool, args)` classify a write/commit touching the files that
   enforce the safety model (`broker.py`, `broker_store.py`, `policy.py`, `council.py`,
   `lenses.py`, `scrum.py`, `daemon.py`, `agent_session.py`, and the grant seed
   `prompts/agents.json`). The council's deterministic **Sentinel** lens turns a match into an
   ESCALATE → the existing park/approve machinery, even for the Builder, regardless of
   reversibility; a human approval of the exact request clears it. The review cockpit is its
   approval surface. Path matching is suffix-on-boundary so relative and absolute (Windows or
   POSIX) forms both match while lookalikes do not. **Closed the last open-decisions row blocking
   an unattended run.**
2. **Verification before `done`.** **Done (general case).** `run_goal_task` no longer trusts a
   doer session's self-reported "done": it calls `verify_goal_done`, which confirms the kind's
   own done-means independently before flipping `DONE`. For `code` the verifier (`run_tests`)
   re-runs the suite through the broker on the built branch and refuses `done` if it is red or
   unrunnable — the task blocks with the failure detail instead (catches the most likely live-fire
   failure: a local model declaring victory early). A `WorkKindSpec.verifier` field carries the
   check, and a doer-bearing kind without a verifier now raises at spawn (a doer can claim done,
   so something else must confirm it). **Remaining:** `browser.verify_local_app` — the frontend
   instance of the same step (reuse `browser_brain.py`'s local CDP primitive); this is the
   last open Milestone A checkbox.
3. **P6 — notify transport + standing grants.** **Standing grants: done.**
   `BrokerStore.create_standing_grant / list_standing_grants / revoke_standing_grant /
   standing_grant_for` + broker integration: a standing grant pre-authorises one `(agent, tool)`
   (optionally args-pinned) to run without parking the risk-model APPROVE gate, rate-capped per day
   via the same `rate_counters` the Optimise lens reads; over the cap it falls back to the human
   park. A pre-authorised action still dispatches and lands in the notify queue (review-after), and
   it **never** bypasses the council floor — a dedicated test asserts the Sentinel gate still
   escalates a self-modification even with a broad standing grant. CLI: `orac standing
   list/add/revoke`.
   **Notify transport: done (passive channels).** `notify.review_queue_summary(store)` turns the
   queue state (unacked notifications + pending approvals) into one operator-facing signal. The
   daemon prints it each tick when non-empty, so an unattended run surfaces its queue instead of
   waiting to be polled; the UI exposes it in `/api/state` (`review_queue`) and a read-only
   `/api/reviews` endpoint mirroring the CLI cockpit. **Optional follow-up:** a true push channel
   (Windows toast) consuming the same summary, and UI buttons to ack/approve from the browser.
4. **Soak run, then choose the next surface.** The exit criterion's stated unknown is live-model
   quality, not machinery. A few daemon-days with 1–3 in place generates the labelled escalation
   data the lens-eval suite wants, and decides what earns the next slot: Group 2 (Communications,
   blocked on the credential vault) or more Group 1 depth (e.g. `browser.verify_local_app`).

---

## Decomposition fan-out — many subagents under one goal

The Orchestrator can break a goal into a fan-out of subagents, each owning a slice of the
intent, with resource governance and an intent-coverage guarantee. Built and tested as a
library; **not yet wired into the daemon loop** (the loop still runs one doer per goal via
`scrum._build_if_goal_task` → `run_goal_task`; switching it to `run_orchestrated_goal` is the
remaining integration step).

- **(a) Subagent register (the ≤500 roster).** `broker_store.subagents` + `MAX_SUBAGENTS=500`.
  `admit_subagent` is fail-closed admission control; `subagent_free_slots` / `active_slice_total`
  expose the live numbers. Distinct from static agent profiles: these are live instances.
- **(b) Intent ledger + parent-stays-open.** `intent_ledger.py` tracks each declared slice
  (sub-intent → child → status) on the parent metadata; `settle_parent_against_ledger` is the
  authoritative gate — covered ⇒ DONE, any slice blocked ⇒ BLOCKED, else the parent stays open
  and Intent logs "Orchestrator not finished". The deterministic floor guarantees no declared
  slice is dropped; semantic coverage is (d)'s judgment. `run_decomposed_goal` runs one child
  per slice; every spawned doer is admitted to the register.
- **(c) The honest abundance frame.** `orchestrator.propose_decomposition` tells the model the
  *live* free-slot count ("you have N of MAX free") to bias toward decomposition. The number is
  read from the register (never hardcoded) and equals the enforced cap, so it self-tightens as
  the roster fills; a plan exceeding its honest budget is refused.
- **(d) The counterweight.** `plan_review.review_decomposition` — Intent/Simple/Efficiency judge
  the *plan* before any spawn (coverage / over-fragmentation / waste), aggregated like the
  council. This is the brake on the sprawl the frame encourages.
- **(e) Both-agree DISPATCH gate.** `dispatch.both_agree` — a spawn fires only if the Orchestrator
  proposed it (approved plan) *and* Optimise admits it (free slot + room in the resource band,
  the 60%-utilisation idea made concrete). A refused spawn defers the slice. `run_orchestrated_goal`
  ties (c)→(d)→(e)→(b) into one entry.

**Next for this subsystem:** wire `run_orchestrated_goal` into the loop behind a goal-size
heuristic (small goals stay single-doer); promote the RETURN edge to a full council review
(today the per-slice return is the deterministic `verify_goal_done` floor); let subagents
recurse (the register cap is already global, so depth is naturally bounded).

---

## Milestone B — Mature the governance, then widen the surface (POST-BOOTSTRAP)

- [x] **P5 — LLM lenses (risk-gated).** Lenses escalate to the model only on consequential edges
      (`LLM_REVIEWED_TOOLS`); cheap edges stay deterministic. Calibrated + scored (`orac lenses
      eval`), live-fire verified. *(Landed during the bootstrap — see Milestone A.)*
- [x] **P6 — Standing grants + notify.** Standing grants short-circuit the human requirement for
      pre-authorised recurring intent (the fish-feeder case), rate-capped via `rate_counters`,
      without ever waiving the safety floor. Notify transport surfaces the queue each daemon tick
      and in the UI state. *(See Build-order item 3. Optional follow-up: a push toast + UI
      ack/approve buttons.)*
- [ ] **Credential vault** (DPAPI / Windows Credential Manager, opaque `credential_ref`,
      redaction at the logging layer). **Hard blocker for Group 2.**

Then the remaining four categories, in **risk order** (lowest first), each gated by the now-mature
risk model. Detail + tool lists in [tool-categories.md](tool-categories.md).

- [ ] **Group 2 — Communications.** `channel.read` then `channel.send`; default **draft → approve
      → send**. Start with Slack *read*. (Blocked on credential vault.)
- [ ] **Group 3 — Media.** Job queue, not blocking calls; ComfyUI; `review → publish`.
- [ ] **Group 4 — Physical.** `read_state / prepare_action / execute_action`; e-stop; cooldowns;
      Home Assistant / MQTT first. Approval by default.
- [ ] **Group 5 — Human Events.** Sessions epic that *consumes* the broker (a workflow layer on
      top, not bundled into it).

---

## Open decisions / dependencies

| Decision | Blocks | Notes |
| --- | --- | --- |
| Code-execution substrate (Roo Code / Codex / local shell) | Group 1 write slice | **Settled:** local subprocess git/pytest. Write = `repo.write_file` (whole file) + `repo.edit_file` (surgical, fail-closed); `repo.apply_patch` satisfied in spirit |
| ESCALATE vs BLOCK semantics (design §8.3) | P3 | **Settled:** ESCALATE→pending, BLOCK→denied |
| Safety-critical-file gate (design §8.7) | unattended daemon run | **Promoted to Build-order item 1** (next): edits to broker/policy/council/loop **and the grant seed** escalate to human even for the Builder |
| Credential vault | Group 2 | No real `channel.send` without it |
| 60% band tolerance + reaction speed (design §8.6) | Optimise driver | Control-loop tuning, not a blocker for Milestone A |
| Group 5 as separate epic | Group 5 | Workflow engine consuming the broker, not part of it |
