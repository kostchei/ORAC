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
- [x] **Group 1 write slice** (Builder only, checkpoint-first, confined to approved repo roots): `git.create_branch`, `repo.write_file`, `git.commit`. *(Substrate: local subprocess git/pytest, no external agent framework. `repo.apply_patch` deferred — `repo.write_file` covers creation for now.)*
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
- [ ] **`browser.verify_local_app`** — verification before a task may reach `done`.

**Exit criterion for Milestone A:** idle ORAC picks a self-improvement task, branches, applies a
patch, runs tests, and opens the change for human approval — end-to-end through the council, with
the Builder as the only writer. **Status: the full circle runs in tests** (idle → originate →
locked READY → Builder session builds on a branch with real files and passing tests → DONE →
loop originates the next goal). Caveat: proven with a scripted model; quality with a live local
model (LM Studio tool-format reliability) is the remaining unknown, not the machinery.

---

## Milestone B — Mature the governance, then widen the surface (POST-BOOTSTRAP)

- [ ] **P5 — LLM lenses (risk-gated).** Lenses escalate to the model only when the risk class
      warrants; cheap edges stay deterministic. Cost/latency guardrails land here.
- [ ] **P6 — Standing grants + notify.** Short-circuit the human requirement for pre-authorised
      recurring intent (the fish-feeder case), rate-capped via `rate_counters`. Notify transport.
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
| Code-execution substrate (Roo Code / Codex / local shell) | Group 1 write slice | Pick before `repo.apply_patch` / `repo.run_tests` land |
| ESCALATE vs BLOCK semantics (design §8.3) | P3 | ESCALATE→pending, BLOCK→denied (proposed) |
| Safety-critical-file gate (design §8.7) | P1/P4 | Edits to broker/policy/council/loop **and the grant seed** escalate to human even for the Builder |
| Credential vault | Group 2 | No real `channel.send` without it |
| 60% band tolerance + reaction speed (design §8.6) | Optimise driver | Control-loop tuning, not a blocker for Milestone A |
| Group 5 as separate epic | Group 5 | Workflow engine consuming the broker, not part of it |
