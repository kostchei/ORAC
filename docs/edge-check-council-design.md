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
   │                        ├─ Optimise (over budget?)
   │                        ├─ Simple   (over-scope?)
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
| **Optimise** | Within budget / the 60% target? | projected spend exceeds budget |
| **Simple** | Minimal path, or over-building? | cheaper/smaller route exists |
| **Efficiency** | Duplicates or wastes existing work? | result already exists / is dead work |

A lens is an interface, not necessarily an LLM call. Cheap lenses can be deterministic
(e.g. Optimise reads the rate counters); expensive lenses escalate to the model only when the
risk class warrants it.

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

## 9. One-line summary

Orchestrator decomposes → subagents do the work → the four agents act as a risk-throttled
review council on every broker-mediated edge → allowed / denied / pending + audit. The JSON
contract accompanies the task, but the broker is where faithful-following becomes
**enforced**-following.
