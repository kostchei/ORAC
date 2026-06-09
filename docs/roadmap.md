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

- [ ] **P0 — Types.** `EdgeKind`, `LensDecision`, `ReviewContext`, `LensVerdict`, `CouncilVerdict` (`models.py`). No behaviour.
- [ ] **P1 — Risk throttle.** `policy.py::risk_class(tool, args) -> (reversibility, externality)` + throttle table. Classify every existing tool. **Retire the hardcoded `APPROVAL_REQUIRED` set** — `fs_read` becomes `local·reversible → auto`.
- [ ] **Builder role + privilege separation.** Add the `builder` agent to `agents.json`; grant it the write capabilities; assert **no reviewer/orchestrator holds a write grant** (a test — this single assertion is the security model of self-modification). (§4.6)
- [ ] **Group 1 read slice** (read-only, `auto`): `repo.read_file`, `repo.search`, `repo.run_tests`.
- [ ] **Group 1 write slice** (Builder only, checkpoint-first): `git.create_branch`, `repo.apply_patch`, `git.stage`, `git.commit`. Writes confined to approved repo roots.
- [ ] **P2 — Council skeleton.** `council.py` with the four lenses as *deterministic* checks; wire into `broker._decide`; per-lens verdicts to audit.
- [ ] **P3 — Aggregation + pending.** Any BLOCK → denied; any ESCALATE → existing pending path. `git.push` / external steps park for approval.
- [ ] **P4 — Subtask contract.** `parent_id` child tasks + `SubtaskContract`; Orchestrator decomposes a self-improvement goal and spawns the Builder.
- [ ] **`browser.verify_local_app`** — verification before a task may reach `done`.

**Exit criterion for Milestone A:** idle ORAC picks a self-improvement task, branches, applies a
patch, runs tests, and opens the change for human approval — end-to-end through the council, with
the Builder as the only writer. At that point the system can start helping build everything below.

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
