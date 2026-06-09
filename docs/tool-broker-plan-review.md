# Review: "Permissioned Tool OS" expansion plan

Review of the proposed Tool Broker / Permission Engine / Credential Broker expansion,
grounded in the current repo (`agents.py`, `agents.json`, `tools/catalog.json`,
`tooling.py`, `scrum.py`, `storage.py`).

## Direction: sound

The spine is correct: *tools are capabilities, permissions are explicit grants, agents
act only through the broker.* Draft-by-default, read-only-first, local-first, human
approval for irreversible/external/physical, no raw tokens to agents, and not starting
messaging with browser automation are all the right conservative calls. The disagreements
below are about sequencing, threat-model honesty, and a few taxonomy/ownership details.

## Key issues

1. **No real tools exist yet.** Every entry in `catalog.json` is a journaling function in
   `RegularToolExecutor` that mutates an in-memory `Task`. None touch an external system.
   The plan is a near-rewrite of the tool layer, not an extension — treat the broker as the
   new foundation, not an add-on.

2. **Tool selection is hardcoded, not agent-driven.** `agents.py::_apply_builtin_action`
   dispatches by agent slug; the `tools: [...]` arrays in `agents.json` are prompt
   decoration, not an enforced allow-list. Prerequisite for any broker: agents must emit
   structured capability requests, and *every* tool call (including the 18 existing ones)
   must route through the broker, or the guarantee leaks through the old path.

3. **Threat model must be explicit.** All agents run in one process with identical trust.
   Per-agent principals and "credentials never exposed to agents" imply isolation that
   doesn't exist — in-process code can import the adapter and bypass the broker. The real
   boundary is *the model's outputs*, not *which agent*. If true isolation is wanted, the
   broker must be a separate process; otherwise call it what it is: a guardrail against
   model mistakes. Restate the credential goal as "secrets never enter a prompt or
   work-log, redacted at the logging layer."

4. **Audit log: design for review/undo, not non-repudiation.** On a single-user box the
   writing process can rewrite `board.json`; tamper-proofing isn't achievable and isn't the
   point. Hash-chain records if you want tamper-evidence cheaply, but the real value is
   "show me what happened and let me reverse it."

5. **Split the risk taxonomy into two axes.** The single ladder conflates reversibility,
   externality, and data sensitivity ("public posting" is both `external_write` and
   `dangerous`; a PTZ nudge and a firmware flash are both `physical_action`). Use
   *reversibility* (reversible / hard-to-reverse / irreversible) × *externality*
   (local / external-private / external-public / financial / physical) and derive approval
   from the pair.

6. **Approval UX is where this lives or dies.** Each risk class needs its own preview
   renderer and "what could go wrong" line; generic previews get rubber-stamped. Add
   batching and scoped/expiring "approve rule". The 24/7 daemon needs a durable
   `pending_approval` task state with timeouts so the loop parks waiting tasks instead of
   spinning — a concrete addition to `models.py`/`scrum.py`.

7. **`supports_dry_run` over-promises.** Most external actions have no true dry-run; define
   it precisely as *validate + render intended effect, no side effects*, and never let a
   dry-run success imply the real call will succeed.

8. **Permission Engine owns grant mutations.** In the diagram the UI writes Grant Store and
   Audit Log directly, skipping the engine — that's two writers and inconsistent policy.
   UI calls the engine; the engine owns grants and the pending-approval queue.

9. **Agent→capability mapping is half-right.** Optimiser→scheduling and Efficiency→grant-GC
   are clean fits. Don't make Intent the permission broker — goal ambiguity ("what does
   done mean?") is a different question from the permission ask ("may I post to #general?"),
   which belongs to the Permission Engine + approval UI.

10. **Omissions that will bite:** `storage.py` is a 48-line flat JSON board; grants, audit,
    pending-approvals, and per-day rate counters are stateful and written concurrently by
    daemon + UI — use SQLite. Add idempotency keys for external sends. Define a stable
    agent-facing result contract (`allowed | denied | pending | error`). Model connector
    health (available only if `credential_ref` resolves and a cheap ping passes; a dead
    connector must never block the loop). Split the human-events/session system into a
    separate epic that *consumes* the broker rather than bundling a workflow engine in.

## Reordered build plan (checklist)

Principle: make it real once, then make it general.

- [ ] Define the agent-facing capability request + result contract
      (`allowed | denied | pending | error`).
- [ ] Make agents emit structured tool-call intents instead of hardcoded dispatch.
- [ ] Build a minimal broker and migrate the 18 existing journaling tools through it
      (zero external risk; validates the interface).
- [ ] Move state to SQLite (grants, audit, pending-approvals, rate counters).
- [ ] Add a durable `pending_approval` task state + timeouts to the scrum loop.
- [ ] Build ONE real read-only adapter end-to-end through the broker
      (filesystem read or Slack read).
- [ ] Generalize: capability manifest + Permission Engine (sole owner of grant mutations).
- [ ] Two-axis risk model (reversibility × externality) driving approval requirement.
- [ ] Credential vault (DPAPI / Windows Credential Manager, opaque `credential_ref`,
      redaction at the logging layer).
- [ ] Approval UI with per-risk-class previews, batching, scoped/expiring rules.
- [ ] Draft-only external tools (Slack draft, WhatsApp draft, Reddit draft).
- [ ] ComfyUI media queue + artifact store (job queue, not blocking calls).
- [ ] Home Assistant / MQTT physical adapter (read_state / prepare / execute, e-stop,
      cooldowns).
- [ ] Connector health/availability model.
- [ ] Standing grants for repeated trusted workflows — last, not first.
- [ ] (Separate epic) human-events/session system consuming the broker.
