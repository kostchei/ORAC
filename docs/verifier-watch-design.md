# The Verifier-Watch Discipline

Spec and design rules for ORAC's human-watching and advisory layer: **Reservoir**, **WIP**, and **Cost Estimation**. This document establishes the architectural contract that governs any component monitoring human interaction, session concurrency, or prospective execution cost.

---

## 1. Background & Naming Note

ORAC's governance council inspects the *action* an agent attempts to execute (via lenses: Intent, Optimise, Simple, Efficiency, Sentinel). The council enforces deterministic floors and LLM cognition to ensure tasks are safe, bounded, and reversible.

However, as ORAC moves toward unattended daemon runs, a critical operational failure mode emerges: **fatigued or rushed human operator review**. An operator clearing a large backlog of `notify` and `escalate` items in a single sitting late at night may approve risky actions or miss important notifications.

In external frameworks (such as `ai-literacy-superpowers`), such monitoring was categorized under a "sentinel" concept. In ORAC, **Sentinel** is already the safety-critical-file escalation lens in `src/orac/council.py`. To eliminate ambiguity and prevent collisions, ORAC designates this human-advisory discipline as **Verifier-Watch** and names its specific surfaces:
- **Reservoir**: Session-span and approval-volume advisory when clearing review queues.
- **WIP**: Concurrency count and shared-worktree collision advisory.
- **Cost Estimation**: Prospective token, time, and measured foundation spend ranges.

---

## 2. The Core Discipline (W1 – W3)

Every verifier-watch component must strictly satisfy three invariant rules:

### W1 — Read-Only
- **Zero mutating writes**: Verifier-watch tools and functions possess no write grants to broker-governed state (`broker.db`, task board, or governance tables).
- **Enforcement**: Covered by automated tests verifying that advisory functions never call mutating broker methods or issue SQL `INSERT`/`UPDATE`/`DELETE` queries against governance tables.
- **Data sources**: Reads exclusively from existing audit and notification tables in `BrokerStore`, local runtime session files in `.orac/sessions/`, and human-declared configuration in `.orac/config.json`.

### W2 — Advisory Only, Never Gates
- **Never blocks or parks**: Advisory outputs are printed precautions presented alongside existing CLI operations (`orac reviews`, `orac approve`, `orac deny`, `orac ack`, `orac rollback`, `orac status`).
- **Zero governance interference**: Advisories never return a `CouncilVerdict`, never change risk classification, never mutate `TaskStatus`, and never halt or park a command. The human operator retains full authority.

### W3 — Honesty Discipline
- **Observed or Declared**: Every statement emitted by a verifier-watch surface must be either:
  1. `Observed`: A literal count, duration, timestamp, or measured spend rate derived directly from state.
  2. `Declared`: A threshold or limit explicitly set by the operator in configuration.
- **No speculative inference**: Verifier-watch components never infer, psychologize, or assign a numerical "fatigue score" or "competence rating" to the operator. It reports objective observations: *"16 queue items cleared in 52 minutes (threshold: 15 items / 45 min)."*

---

## 3. Anti-Patterns to Avoid

1. **Never score or profile the human**: Do not model operator fatigue as a probabilistic or synthetic score.
2. **Never persist records *about* the human's state**: Runtime state tracks processes, sessions, and timestamps. No table or schema may record evaluations of human operator performance.
3. **Never allow advisories to become automatic gates**: If an advisory condition is met, print the notification clearly, but proceed with the operator's requested action immediately without blocking.
4. **Never fabricate ungrounded point estimates**: In prospective estimation, always return bounded ranges `(low, high)`. If measured rate data is unavailable, explicitly state that dollar figures are unmeasured rather than inventing costs.

---

## 4. Architectural Surfaces

### 4.1 Reservoir Advisory (`src/orac/notify.py`)
- Emitted when an operator executes queue-clearing commands (`approve`, `deny`, `ack`, `rollback`).
- Analyzes consecutive human-principal audit records in `broker_store`.
- Groups actions into a single session based on an idle gap cutoff (e.g. 20 minutes).
- Emits a warning when either the item count or sitting duration crosses declared thresholds (`reservoir.count_threshold`, `reservoir.span_minutes`).

### 4.2 WIP & Worktree Advisory (`src/orac/session_registry.py`)
- Tracks active CLI and daemon sessions via local, gitignored runtime records (`.orac/sessions/<pid>-<timestamp>.json`).
- Automatically prunes stale records (dead PIDs or expired TTL).
- Detects the specific high-risk operational condition: **multiple concurrent sessions operating in the exact same working tree directory**, which risks Git branch and working copy collisions.
- Emits warnings in `orac status` and at daemon startup.

### 4.3 Prospective Cost Estimation (`src/orac/cost_estimate.py`)
- Provides `orac estimate "<goal description>"`.
- Produces token ranges, latency ranges, and — when `usage.json` contains measured foundation spend — a dollar cost range.
- Strictly refuses to guess dollar amounts when only free/local models or unmeasured tokens are used.

---

## 5. Verification Matrix

| Component | Invariant Tested | Test Location |
| :--- | :--- | :--- |
| **Reservoir** | W1 (Read-Only), W2 (Non-blocking), W3 (Threshold honesty) | `tests/test_reservoir_advisory.py` |
| **WIP Registry** | W1 (Isolated state), W2 (Advisory-only), Worktree collision detection | `tests/test_session_registry.py` |
| **Cost Estimate** | W3 (Ranges only, no ungrounded cost guessing) | `tests/test_cost_estimate.py` |
