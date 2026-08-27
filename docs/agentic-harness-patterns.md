# Agentic Harness Patterns: Prewalk, Role Isolation, and the Promoter Lifecycle

Implemented design notes incorporating insights from [Scott Fryxell's *The Harness Is the Thing*](https://scott-fryxell.github.io/blog/the-harness-is-the-thing/), Can Bölük's **Prewalk** methodology, and the **Planner / Worker / Critic / Promoter** agentic lifecycle. The implementation references below describe code of record; the status table in §4 names the regression tests.

---

## 1. Core Thesis: The Harness Outlives the Models

Models are commodifying rapidly. Local models (`mistral-small-3.1`, `qwen3.6-coder`, `gemma-4`), cost-effective APIs (`deepseek`), and rotating frontier providers all shift in price, availability, and capability.

The durability and leverage of an autonomous software system reside in its **harness**:
- **Deterministic state & governance**: OS file locking, append-only event logs, SQLite audit databases, and deterministic council floors.
- **Role & Context Isolation**: Separation of distinct cognitive tasks (planning, building, critiquing, broadcasting) into isolated contexts rather than a single self-rationalizing prompt.
- **Fail-closed boundaries**: Review-after queues, one-step rollbacks, and explicit slice contracts.

ORAC is architected around this principle: the brain in the model slot is interchangeable; the broker, board durability, verifier-watch layer, intent ledger, and Promoter spool are the invariant harness.

---

## 2. Pattern 1: Prewalk & Frontier Pattern-Setting

### 2.1 The Problem
Running large models on every single subtask is cost-inefficient and exhausts rate limits. Conversely, running smaller or local models on open-ended architectural planning often results in hallucinated dependencies, architectural sprawl, or malformed decompositions.

### 2.2 The Prewalk Solution
1. **Frontier Exploration & Planning**: A high-leverage model (frontier API or rotated browser foundation) explores the problem space and decomposes the goal into an explicit **Directed Acyclic Graph (DAG)** of subtasks.
2. **Pattern-Setting on Node 0**: Instead of immediately handing off the entire plan, the frontier model implements **Subtask 0** — establishing the core interfaces, unit test fixtures, type contracts, and reference implementation.
3. **Local Worker Handoff**: Once the pattern is set and proven by passing tests on Node 0, local/smaller worker models (e.g. LM Studio `mistral-small` or `qwen-coder`) execute Nodes 1 through N by matching the established code style and test conventions.

### 2.3 ORAC Implementation

`Scrum` enables Prewalk when model routing is active. `run_orchestrated_goal` marks slice 0 as `pattern_setter`, and that field survives the durable intent ledger. The pattern-setting slice runs on the foundation planning brain; later slices run on the local child brain.

Execution is sequential on the same governed worktree. After slice 0 verifies, ORAC records its observed Git `HEAD` and completion summary in `parent.metadata["prewalk"]`. Later contracts receive `pattern_commit_sha` and `pattern_summary`, while physically inheriting the interfaces, fixtures, and reference implementation already present in the worktree. If the pattern setter fails, dependent slices are not dispatched.

---

## 3. Pattern 2: The Four-Stage Agentic Lifecycle

```
 ┌─────────────┐       ┌────────────┐       ┌────────────┐       ┌──────────────┐
 │   PLANNER   │ ────> │   WORKER   │ ────> │   CRITIC   │ ────> │   PROMOTER   │
 │ (Explores & │       │(Implements │       │(Simplifies │       │(Broadcasts & │
 │  builds DAG)│       │ 1 DAG node)│       │& validates)│       │  syncs docs) │
 └─────────────┘       └────────────┘       └────────────┘       └──────────────┘
                              ▲                    │
                              └──── (Reject/Loop) ─┘
```

A single prompt that plans, executes, and critiques itself suffers from objective confusion and context exhaustion. Isolating these four roles produces clean boundaries.

### 3.1 Planner (Intent & Decomposition)
- **Role**: Map high-level intent into an explicit DAG of verifiable tasks.
- **ORAC Mapping**: `intent_backbone.py`, `decomposition.py`, `plan_review.py`.
- **Invariants**: Must produce explicit acceptance criteria, reversibility classification, and dependency ordering.

### 3.2 Worker (The Builder)
- **Role**: Implement one DAG node at a time against an explicit slice contract.
- **ORAC Mapping**: `work.py` (`Builder` agent).
- **Invariants**: Strict slice scope; no edits outside owned paths; must pass self-tests before returning.

### 3.3 Critic (Council & Lens Review)
- **Role**: Question assumptions, simplify complexity, and challenge changes before acceptance.
- **ORAC Mapping**: Council lenses (`Intent`, `Optimise`, `Simple`, `Efficiency`, `Sentinel`), `lenses.py` LLM review, `plan_review.py`, and `self_tune.py`.
- **Invariants**: Veto aggregation; "I cannot judge this" escalates rather than passing; pushes back to Worker on regression or excessive churn. Before a `git.commit` dispatch, the Simple lens receives a bounded, read-only diff of the explicitly named paths, including untracked text files. It can therefore challenge needless complexity before the commit is created rather than judging only its message.

### 3.4 Promoter (Communication & Documentation Sync)
- **Role**: *"A job is not complete until you've properly communicated it to others."*
- **ORAC Mapping**: `src/orac/promoter.py`, the Comms / Messenger subsystem, `TODO.md` / `docs/roadmap.md` reconciliation, the promotion spool under `.orac/promotions/`, and the Slack/WhatsApp chat gateways.
- **Invariants**: Runs only after the goal reaches `DONE`; writes one idempotent digest per task; never guesses which checkbox a change satisfies. Checkbox reconciliation is exact and opt-in through `task.metadata["promoter_checkboxes"]`. A Promoter failure is logged and makes the goal visibly `BLOCKED` rather than silently skipping communication.

---

## 4. Implementation Status

| Milestone | Status | Code of Record | Regression Coverage |
| :--- | :--- | :--- | :--- |
| **M1: Prewalk DAG Execution** | Implemented | `decomposition.py`, `intent_ledger.py`, `work.py`, `scrum.py` | `test_prewalk_runs_pattern_setter_first_and_inherits_commit` |
| **M2: Critic Simplification Pass** | Implemented | `lenses.py` supplies the Simple lens the proposed named-path diff before commit dispatch | `test_simple_lens_sees_named_diff_before_commit` |
| **M3: The Promoter Stage** | Implemented | `promoter.py`; `Scrum._promote_if_done`; `chat_gateway.py` outbound completion digests | `tests/test_promoter.py` |
| **M4: Verifier-Watch Integration** | Implemented | `notify.py::operator_advisory_summary`, consumed by status, daemon, and chat surfaces | `tests/test_notify.py`, `tests/test_promoter.py` |

Phase 5's entropy work is also executable rather than catalogue-only: `entropy.py` performs bounded runtime garbage collection and produces evidence-backed maintenance findings for `driver.py`. The catalogue remains the policy and prioritisation reference.

---

## 5. Summary Principle

The harness provides the structural rails:
1. **Frontier sets the pattern (Prewalk)**.
2. **Worker builds the slice**.
3. **Critic guards the floor**.
4. **Promoter finishes the job by making it visible**.

The boundaries are fail-visible: a failed pattern setter stops dependent workers, an unusable Critic verdict escalates, and a failed Promoter blocks completion. No role silently waves work into the next stage.
