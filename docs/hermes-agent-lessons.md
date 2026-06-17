# Hermes Agent lessons for ORAC

Hermes Agent is the closest current open-source reference point for ORAC's direction: a self-hosted personal agent with persistent memory, reusable skills, gateways, scheduled work, subagents, browser/tool use, multiple execution backends, and trajectory export. This note records what ORAC should borrow, what it should not copy, and the concrete follow-up slices that preserve ORAC's governance-first design.

## ORAC baseline

ORAC is already not a blank slate. It has:

- A local task board and delivery loop, with the daemon able to originate and run work continuously.
- A broker/council/risk spine where every capability request is adjudicated before dispatch.
- A review-after queue for reversible code work, with rollback for git-backed changes.
- Standing grants for recurring pre-authorised intent, without bypassing the council floor.
- A chat control plane for operating ORAC from Slack/WhatsApp without granting agents messaging powers.
- A subagent register and decomposition library, already capped at the same number the Orchestrator is told.
- Board event history plus broker audit tables, which together are the raw material for memory and trajectory export.

So the integration target is not "adopt Hermes as the runtime". The useful move is to borrow the mature product surfaces and map them onto ORAC's stricter broker, council, and review-after semantics.

## Hermes features worth borrowing

### 1. Skills as durable learned operating procedures

Hermes treats skills as reusable, agent-readable instructions that can be created from experience and loaded again later. ORAC should copy the *artifact shape*, not automatic uncontrolled behaviour.

Recommended ORAC shape:

- Store skills as local Markdown files under a governed skill library, e.g. `.orac/skills/` or `skills/`.
- Use a portable `SKILL.md`-style convention: title, purpose, trigger conditions, allowed tools, preconditions, procedure, verification, rollback, and examples.
- Let Builder or a future Memory Curator propose skills after repeated successful patterns, but land them through review-after as ordinary repo/doc changes.
- Treat any skill loader, skill manifest, or built-in privileged skill as safety-critical for Sentinel.
- Prefer skills as *playbooks* first; do not let skills install code, widen tool grants, or hide broker calls.

Near-term slice: `docs/skill-system-design.md` plus a read-only skill loader that only lists and renders local skill docs.

### 2. Scheduler / cron as first-class recurring intent

Hermes exposes cron/scheduled automations as a normal part of the agent. ORAC has a daemon and standing grants, but not a first-class schedule object yet.

Recommended ORAC shape:

- Add `scheduled_goals` to broker state or a dedicated scheduler store.
- CLI/UI: `orac schedule add/list/revoke`.
- The daemon checks due schedules and creates ordinary tasks; it does not run tools directly from the scheduler.
- Each scheduled task still goes through Intent, broker, council, risk model, standing grants, notifications, and review-after.
- Standing grants can pre-authorise the recurring *tool call* once the scheduled task exists, but cannot bypass Sentinel or rate/fair-share checks.

Near-term slice: scheduler data contract and a daemon tick that materializes due read-only/code goals as tasks.

### 3. Gateway as operator control, not extra agent powers

Hermes supports several messaging gateways. ORAC already has a chat control plane and correctly separates it from Group 2 Communications.

Recommended ORAC shape:

- Keep chat as an operator transport peer to CLI/UI.
- Do not let the gateway become a second autonomous runtime.
- Reuse Hermes' product lesson: one identity/account can resume state across channels.
- Keep destructive commands grammar-bound: exact `approve`, `deny`, `ack`, and `rollback` IDs.
- Agent-sent communications remain Group 2 and stay blocked on the credential vault.

Near-term slice: improve chat control plane continuity/status, not agent messaging.

### 4. Isolated subagents / swarms, but bounded by ORAC's counterweights

Hermes supports parallel subagents. ORAC already has a subagent register, intent ledger, and decomposition review, but the daemon still defaults to one doer per goal.

Recommended ORAC shape:

- Wire `run_orchestrated_goal` into the loop behind a goal-size heuristic.
- Keep small goals single-doer.
- Keep the global `MAX_SUBAGENTS` cap honest: the prompt's free-slot count must always equal the enforced register count.
- Require plan review before fan-out: Intent for coverage, Simples for over-fragmentation, Efficiency for waste.
- Promote RETURN edges to council review only after deterministic verification remains green.

Near-term slice: route large code goals through the existing orchestrated path, with tests proving small goals remain single-doer.

### 5. Memory and trajectory export from existing ORAC history

Hermes' memory and training/export features point at a useful ORAC seam: ORAC already records board revisions, work logs, reviews, notifications, and broker audit rows.

Recommended ORAC shape:

- Do not start with vector memory or opaque embeddings.
- First build a Memory Curator that turns completed tasks into structured summaries: goal, constraints, actions, tools, verdicts, tests, rollback availability, outcome.
- Store local curated memory in SQLite with explicit provenance to board revision and audit IDs.
- Add `orac memory search` as a read-only tool before any agent auto-consumes memory.
- Add trajectory export after that, preferably as JSONL/ShareGPT-like examples suitable for lens calibration and local model fine-tuning.

Near-term slice: export review/lens trajectories from existing broker tables; no new model calls.

### 6. Execution backends stay adapters, not a runtime swap

Hermes supports several execution backends. ORAC should keep its current local git/pytest/browser primitives as the safe base.

Recommended ORAC shape:

- Add Docker/SSH/remote backends only as broker-mediated tool adapters.
- Classify each backend by reversibility and externality in `policy.py` before enabling it.
- Never auto-discover execution targets.
- Require explicit approved roots, branches, logs, tests, and review-after notifications.

Near-term slice: none until current local Builder live-model quality is proven by soak runs.

## Priority order for ORAC

1. **Skill library contract** — lowest risk, immediately useful, improves prompts without broadening powers.
2. **Scheduler contract** — builds on existing daemon + standing grants, but materializes tasks instead of bypassing governance.
3. **Wire orchestrated goals into the loop** — ORAC already has most pieces; this is the Hermes subagent lesson with ORAC's brakes.
4. **Memory curator + trajectory export** — turns board/audit history into reviewable knowledge and calibration data.
5. **Gateway polish** — cross-channel continuity for the operator, not agent messaging.
6. **Extra execution backends** — defer until local Builder quality is proven.

## Non-goals

- Do not replace ORAC's broker/council/risk model with Hermes' runtime.
- Do not auto-install community skills.
- Do not let skills expand tool grants.
- Do not let scheduled jobs call tools directly.
- Do not merge chat control plane with agent communications.
- Do not add new remote execution backends before the local code-writing loop has soak evidence.
