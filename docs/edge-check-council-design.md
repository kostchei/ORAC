# Engineering Design: The Edge-Check Council

**Status:** Proposed · **Owner:** ORAC core · **Supersedes the "pipeline" reading of the four agents**

## 1. Purpose

Move ORAC explicitly off the implicit *trusting pipeline* (five agents mutate one
shared `Task` in sequence and are trusted to honour it) and onto an
**edge-check council**:

- **Subagents do the work.** The Orchestrator spawns isolated subagents to act on a task.
- **The four — Intent, Optimise, Simple, Efficiency — are a review council, not stages.**
  They are interposed on the *edges* of the call graph (orchestrator→subagent,
  subagent→tool, tool→tool, subagent→return), not as nodes the task flows through.
- **The council is throttled by risk.** Reversible/local edges wave through; consequential
  edges convene the council; irreversible/external edges may also require a human.
- **The broker is the enforcement chokepoint** where the council's judgment is applied,
  recorded, and (when needed) parked for approval.

This is depth-agnostic: because the check attaches to edges rather than levels, it applies
identically however deep a subagent call sits. ORAC therefore does not inherit OpenClaw's
flat-hierarchy limitation.

## 2. Current state (what exists today)

| Concern | Where | Shape today |
| --- | --- | --- |
| Capability contract | `models.py` | `CapabilityRequest` / `CapabilityResult` (`allowed/denied/pending/error`) |
| Enforcement chokepoint | `broker.py::ToolBroker` | one entry point; allow-list check; adapter vs journaling dispatch |
| Durable state | `broker_store.py::BrokerStore` | SQLite: grants, audit, pending_approvals, rate_counters |
| Approval path | `broker.py` + `scrum.py` | `pending` verdict → `Task.park_for_approval` → loop resume |
| Real tool | `adapters.py::fs_read` | first adapter that touches an external system |
| The four agents | `agents.py` + `prompts/*` | deterministic `_apply_builtin_action`, sequential, trusted |
| Loop | `scrum.py::Scrum.run` | iterates tasks, runs all agents per task, resumes parked tasks |

**Gap this doc closes:** the broker's policy is a one-line allow-list check, and the four
agents run as a fixed sequence that trusts each prior stage. We replace that thin policy
with a **risk-throttled, multi-lens review at the broker edge**, and reframe the four agents
as the lenses of that review.

## 3. Target architecture

```
Orchestrator
   │  decompose → spawn
   ▼
Subagent (doer, isolated context)
   │  emits CapabilityRequest
   ▼
ToolBroker  ── edge ──►  Council (risk-throttled)
   │                        ├─ Intent   (goal drift?)
   │                        ├─ Optimise (fair share — near 60%, never idle?)
   │                        ├─ Simple   (rebuild-or-keep given current shape?)
   │                        └─ Efficiency (redundant/wasteful?)
   │   verdict = aggregate(lenses)        ▲
   ├─ allowed  → dispatch (adapter / executor)
   ├─ denied   → blocked, with reason
   ├─ pending  → park + (human or higher council)
   └─ every decision → audit
```

Separation of responsibilities:

- **Broker = plumbing.** Chokepoint, dispatch, audit, pending queue. Mechanism, no judgment.
- **Council = policy.** The deliberative judgment that turns a request into a verdict.
- **Risk model = throttle.** Decides *how much* council an edge convenes (and whether a human
  is also required). This is the home for the two-axis (reversibility × externality) model.

## 4. Core concepts

### 4.1 Edge

A boundary crossing that the broker mediates. Each carries an `EdgeKind`:

| EdgeKind | Example | Default lenses |
| --- | --- | --- |
| `dispatch` | orchestrator → subagent | Intent, Simple, Optimise |
| `tool_call` | subagent → tool/adapter | risk-dependent (see throttle) |
| `tool_chain` | tool → tool (output of one feeds another) | Efficiency, Intent |
| `return` | subagent → parent (result roll-up) | Intent, Efficiency |

Not every lens fires on every edge; the EdgeKind selects the default lens set, and the risk
class can widen or narrow it.

### 4.2 Lens

One reviewer. A lens receives a `ReviewContext` and returns a `LensVerdict`. The four:

| Lens | Question | Veto when |
| --- | --- | --- |
| **Intent** | Does this still serve the locked goal? | action diverges from acceptance criteria |
| **Optimise** | Are we using our fair share — near 60%, never idle, never over? | utilisation drifts off the band (idle **or** overspend) |
| **Simple** | If we rebuilt this now, given its current patched shape, would we build it this way or differently? | a from-scratch rebuild beats continuing to patch |
| **Efficiency** | Duplicates or wastes existing work? | result already exists / is dead work |

A lens is an interface, not necessarily an LLM call. Cheap lenses can be deterministic
(e.g. Optimise reads the rate counters); expensive lenses escalate to the model only when the
risk class warrants it.

Two of these have non-obvious impetus, easy to mischaracterise:

- **Optimise is a two-sided utilisation governor, not a cost cap.** Its impetus is to use a
  *fair share* of resources 24/7 — never too much, but **never idle**. The target is to keep
  utilisation near 60% continuously. So it vetoes overspend *and* flags idleness; on an edge it
  asks "is this a fair use?", but it also has a generative side (§4.2.1).
- **Simple is the rebuild-or-keep test, not just "fewest steps."** It asks: given what we know
  now and the thing's *current accreted shape (with all its patches)*, would we build it this
  way or differently? It vetoes when a from-scratch rebuild would beat continuing to patch.
  This is distinct from Efficiency, which hunts local waste rather than judging the whole shape.

### 4.2.1 Optimise has a second, generative role

A pure edge-check council is **reactive** — it reviews proposed actions. But Optimise's "never
idle" impetus is **proactive**: when the board is under-utilised it should *initiate* work to
fill the 60% band, not wait to be asked. So Optimise is dual:

1. **As a lens** (reactive): on an edge, "is this a fair use of our share — not wasteful?"
2. **As a driver** (proactive): in the 24/7 loop, "are we idle / below the band? then pull or
   spawn more work toward 60%." This is a loop-level role, not an edge review.

This makes Optimise the **utilisation governor of the whole system** — the thing that keeps the
daemon busy-but-not-overcommitted between tasks. The driver side belongs in `scrum.py`'s loop
(it generates/pulls work), while the lens side lives in the council like the others.

### 4.2.2 "60%" is a fair share of *available* capacity, across every resource

The target is not a single metric. It is ~60% of **whatever the system has access to**, per
resource dimension:

- token / $ spend (already tracked by `model_policy.py` foundation cap)
- CPU cycles, memory, GPU/VRAM, disk space, wall-clock time
- any other measurable resource the host exposes

Two properties define it:

1. **Per-resource, not aggregate.** Each dimension has its own 60% band. Optimise should be
   pushing work to fill *each* resource toward its share, and the binding constraint at any
   moment is whichever dimension is tightest.
2. **Against *available*, not total — and available is dynamic.** The denominator is
   currently-free capacity, which **shrinks when external demand crunches the box** (the
   machine gets used for other things). Optimise targets 60% of the *live headroom* and
   **yields** as that headroom contracts: if another workload claims CPU/RAM/disk, ORAC's
   absolute usage drops to stay at 60% of what remains, rather than fighting for the box.

`resources.py::ResourceSnapshot` already measures most of this (`cpu_percent`,
`memory_available_gb`, `vram_percent`, `disk_free_gb`, `busy`, `recommended_tier`), and
`model_policy.py` already throttles model tier off it. So the driver reads the live snapshot
each loop, computes per-resource headroom, and pulls/sheds work to track 60% of available — the
infrastructure to measure "available" largely exists; what's new is the governor that acts on it.

### 4.2.3 The idle default: ORAC works on ORAC

When the board is empty and headroom exists, the driver does **not** invent arbitrary work.
The standing backlog is **self-improvement**: with no external query, ORAC investigates its own
system, reviews its own code, and works to test, secure, and harden itself. That is the default
intent, so it does not read as goal-drift to the Intent lens — self-improvement *is* the goal in
the absence of another.

Two consequences fall out of this:

1. **ORAC dogfoods its own broker.** Self-edits are ordinary tool calls (`write_code`,
   `run_tests`, `git_commit`, …) routed through the same broker + council. Self-modification is
   made reversible by **checkpoint-first** (work on a branch / commit before changing files), so
   under the risk model it is `auto + notify`, not blocked — while `git_push`, releases, or any
   external/irreversible step still gates. The safety model that governs external action governs
   self-modification too; there is no privileged self-edit path.
2. **"Never idle" must not become "always thrashing."** Filling the 60% band with self-work is
   only useful if the work is real. Simple's rebuild-or-keep test and Intent's goal check are
   the brakes: they veto churning a working subsystem just to look busy. Optimise wants the
   capacity used; Simple/Intent ensure it is used on something worth doing. That tension is
   intended, not a bug.

### 4.3 Verdict aggregation

Four reviewers, one decision. **Any blocker is a stop, not a vote to be averaged:**

```
if any lens.verdict == BLOCK      → denied  (or pending, if the blocker is "needs approval")
elif any lens.verdict == ESCALATE → pending (convene human / higher council)
else                              → allowed
```

Consistent with the project's "no fallbacks — throw an error" rule: a vetoed edge stops with
the vetoing lens's reason recorded; it does not silently pass.

### 4.4 Risk throttle

`risk_class(tool, args) -> (reversibility, externality)` drives two dials:

1. **Council depth** — how many lenses, and whether they run as deterministic checks or full
   LLM reviews.
2. **Human requirement** — whether `allowed`-by-council still becomes `pending` for a human.

| Risk | Council | Human |
| --- | --- | --- |
| reversible · local | none (audit only) | no |
| reversible · external | Intent + Efficiency, deterministic | notify |
| hard-to-reverse | full four lenses | per policy (see approval design) |
| irreversible · external/financial/physical | full four lenses | yes — unless a standing grant covers it |

Standing grants (see `docs` approval design) short-circuit the human requirement for
pre-authorised recurring intent, rate-capped via `rate_counters`.

### 4.5 Subagent isolation & the accompanying contract

A subagent receives an explicit **task contract** (the Roo/OpenClaw lesson: instruction-in,
summary-out — the child does not inherit the parent's full history). The contract is the JSON
that accompanies the task, but it is **enforced at the edge, not trusted**:

- **Down:** `SubtaskContract { goal, acceptance_criteria, budget, scope_constraints, parent_id }`.
- **Up:** the subagent returns a `result summary`; the `return` edge runs the council
  (Intent: does the summary meet the criteria? Efficiency: any waste introduced?) before the
  parent integrates it.

Subtasks are modelled as child `Task`s on the existing `Board` with a `parent_id`, so the
council and broker apply unchanged at any depth.

## 5. Data contracts (new types)

```python
class EdgeKind(StrEnum):
    DISPATCH = "dispatch"
    TOOL_CALL = "tool_call"
    TOOL_CHAIN = "tool_chain"
    RETURN = "return"

class LensDecision(StrEnum):
    PASS = "pass"
    BLOCK = "block"       # hard veto
    ESCALATE = "escalate" # needs human / higher council

@dataclass(frozen=True)
class ReviewContext:
    edge: EdgeKind
    request: CapabilityRequest
    task: Task            # carries the SubtaskContract in metadata
    risk: RiskClass

@dataclass(frozen=True)
class LensVerdict:
    lens: str             # "Intent" | "Optimise" | "Simple" | "Efficiency"
    decision: LensDecision
    reason: str

@dataclass(frozen=True)
class CouncilVerdict:
    status: CapabilityStatus       # allowed | denied | pending
    lenses: tuple[LensVerdict, ...]
    reason: str
```

`CouncilVerdict` is what the broker turns into a `CapabilityResult`; the per-lens verdicts are
written to the audit log so every block is explainable and reversible.

## 6. Mapping to existing code

| Change | File | Note |
| --- | --- | --- |
| Add `EdgeKind`, lens/verdict types | `models.py` | extends the existing capability contract |
| `Council` + `Lens` interface | new `council.py` | four built-in lenses reusing the agents' prompts |
| `risk_class()` + throttle table | new `policy.py` | the two-axis model from the approval design |
| Broker calls council instead of bare allow-list | `broker.py::_decide` | allow-list becomes one input to the council, not the whole policy |
| Persist per-lens verdicts | `broker_store.py` | extend `audit` (or a `reviews` table) |
| Subtask contract + child tasks | `models.py`, `agents.py` | `parent_id`, `SubtaskContract` in metadata; Orchestrator spawns |
| Loop runs Orchestrator+subagents, not fixed 4-in-sequence | `scrum.py` | the four stop being a sequence; they are invoked as lenses by the broker |

The allow-list does **not** go away — it remains the cheapest lens (capability granted at
all?) and runs first. The council is what we add *above* it for consequential edges.

## 7. Build plan (phased)

Principle, unchanged from the broker work: **make it real once on a narrow edge, then generalise.**

- [ ] **P0 — Types.** `EdgeKind`, `LensDecision`, `ReviewContext`, `LensVerdict`, `CouncilVerdict`. No behaviour.
- [ ] **P1 — Risk throttle.** `policy.py::risk_class` + throttle table; classify the existing tools. Broker consults it (today: only `fs_read` is gated — replace the hardcoded set).
- [ ] **P2 — Council skeleton + deterministic lenses.** `Council.review(ctx)` with the four lenses as *deterministic* checks (Optimise reads rate counters, Efficiency checks for duplicate audit rows, etc.). Wire into `broker._decide`. Per-lens verdicts to audit.
- [ ] **P3 — Aggregation + pending integration.** Any BLOCK → denied; any ESCALATE → existing pending path. Prove a council ESCALATE parks a task end-to-end (reuse the approval machinery).
- [ ] **P4 — Subtask contract.** `parent_id` child tasks + `SubtaskContract`; Orchestrator decomposes and spawns one subagent; `return` edge runs the council on the summary.
- [ ] **P5 — LLM lenses (opt-in by risk).** Lenses escalate to the model only when the risk class calls for full review; cheap edges stay deterministic. Cost/latency guardrails here.
- [ ] **P6 — Standing grants + notify.** Short-circuit the human requirement for pre-authorised recurring intent; rate-capped. (Joins the approval design.)

Each phase keeps the suite green and the existing deterministic flow working (the four still
reach a verdict; they just reach it as lenses).

## 8. Risks & open decisions

1. **Cost/latency is the make-or-break.** Four LLM lenses on every gated edge will run away.
   The risk throttle (P1) must land *before* LLM lenses (P5), so the cheap majority of edges
   never convene the model. Non-negotiable ordering.
2. **In-process isolation is not real isolation.** Subagents share the process; the contract
   is enforced at the broker edge, not by a sandbox. This is a guardrail against model
   mistakes, not a security boundary (same honesty as the broker review). True isolation =
   separate process, deferred until a use case demands it.
3. **Verdict semantics for ESCALATE vs BLOCK.** Is "needs approval" a BLOCK that the human
   clears, or its own ESCALATE lane? Current proposal: ESCALATE → pending; BLOCK → denied
   (terminal unless re-planned). Confirm before P3.
4. **Which lenses are deterministic vs LLM, per EdgeKind.** Drafted in §4.1/§4.4; needs a pass
   once real tools beyond `fs_read` exist.
5. **Does the Orchestrator itself pass through the council?** Decomposition is an edge too
   (`dispatch`). Proposal: yes — Intent/Simple/Optimise review the *plan* before subagents spawn.
6. **Where does Optimise's driver side live?** The generative "never idle" role (§4.2.1) is a
   loop concern in `scrum.py`, separate from the lens. (What 60% *measures* is settled in
   §4.2.2; what fills idle capacity is settled in §4.2.3: self-improvement.) Remaining open
   sub-questions: the band tolerance (how far off 60% before the driver acts or the lens vetoes)
   and the control loop's reaction speed (avoid thrash as external load oscillates).
7. **Self-modification of safety-critical files needs a higher gate.** §4.2.3 lets idle ORAC
   edit its own code under `auto + notify` (reversible via checkpoint). But edits to the files
   that *enforce* the safety model — `broker.py`, `policy.py`, the council, the loop — are a
   different class: the system rewriting its own governor. Proposal: changes touching those
   paths escalate to `pending` (human) and must pass the existing suite before merge, regardless
   of reversibility. A feedback loop that can weaken its own brakes should not be `auto`.

## 9. One-line summary

Orchestrator decomposes → subagents do the work → the four agents act as a risk-throttled
review council on every broker-mediated edge → allowed / denied / pending + audit. The JSON
contract accompanies the task, but the broker is where faithful-following becomes
**enforced**-following.
