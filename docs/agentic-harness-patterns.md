# Agentic Harness Patterns: Prewalk, Role Isolation, and the Promoter Lifecycle

Design notes and implementation roadmap incorporating insights from [Scott Fryxell's *The Harness Is the Thing*](https://scott-fryxell.github.io/blog/the-harness-is-the-thing/), Can Bölük's **Prewalk** methodology, and the **Planner / Worker / Critic / Promoter** agentic lifecycle.

---

## 1. Core Thesis: The Harness Outlives the Models

Models are commodifying rapidly. Local models (`mistral-small-3.1`, `qwen3.6-coder`, `gemma-4`), cost-effective APIs (`deepseek`), and rotating frontier providers all shift in price, availability, and capability.

The durability and leverage of an autonomous software system reside in its **harness**:
- **Deterministic state & governance**: OS file locking, append-only event logs, SQLite audit databases, and deterministic council floors.
- **Role & Context Isolation**: Separation of distinct cognitive tasks (planning, building, critiquing, broadcasting) into isolated contexts rather than a single self-rationalizing prompt.
- **Fail-closed boundaries**: Review-after queues, one-step rollbacks, and explicit slice contracts.

ORAC is architected around this principle: the brain in the model slot is interchangeable; the broker, board durability, and verifier-watch layer are the invariant harness.

---

## 2. Pattern 1: Prewalk & Frontier Pattern-Setting

### 2.1 The Problem
Running large models on every single subtask is cost-inefficient and exhausts rate limits. Conversely, running smaller or local models on open-ended architectural planning often results in hallucinated dependencies, architectural sprawl, or malformed decompositions.

### 2.2 The Prewalk Solution
1. **Frontier Exploration & Planning**: A high-leverage model (frontier API or rotated browser foundation) explores the problem space and decomposes the goal into an explicit **Directed Acyclic Graph (DAG)** of subtasks.
2. **Pattern-Setting on Node 0**: Instead of immediately handing off the entire plan, the frontier model implements **Subtask 0** — establishing the core interfaces, unit test fixtures, type contracts, and reference implementation.
3. **Local Worker Handoff**: Once the pattern is set and proven by passing tests on Node 0, local/smaller worker models (e.g. LM Studio `mistral-small` or `qwen-coder`) execute Nodes 1 through N by matching the established code style and test conventions.

### 2.3 ORAC Implementation Plan
- In `src/orac/decomposition.py` and `src/orac/scrum.py`:
  - When `route_models=True`, tag `subtask[0]` as `pattern_setter`.
  - Dispatch `subtask[0]` with `foundation_brain_for(policy, "plan")`.
  - Subsequent subtasks (`subtask[1..N]`) inherit the resulting test harness and commit SHA, and execute via the standard local Builder brain.

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
- **ORAC Mapping**: Council lenses (`Intent`, `Optimise`, `Simple`, `Efficiency`, `Sentinel`), `lenses.py` LLM review, and `self_tune.py`.
- **Invariants**: Veto aggregation; "I cannot judge this" escalates rather than passing; pushes back to Worker on regression or excessive churn.

### 3.4 Promoter (Communication & Documentation Sync)
- **Role**: *"A job is not complete until you've properly communicated it to others."*
- **The Gap**: Autonomous systems and focused developers share a common failure mode: committing code and immediately moving on, leaving documentation stale and stakeholders uninformed.
- **ORAC Mapping**: Comms / Messenger subsystem, `TODO.md` reconciliation, `docs/gc-catalogue.md`, and chat gateways.

---

## 4. Architectural Roadmap for ORAC

| Milestone | Component | Target Implementation |
| :--- | :--- | :--- |
| **M1: Prewalk DAG Execution** | `src/orac/scrum.py` | Route Subtask 0 to high-leverage brain to establish reference patterns; dispatch remaining subtasks to local LM Studio. |
| **M2: Critic Simplification Pass** | `src/orac/lenses.py` | Enhance the `Simple` lens post-build to critique code complexity and recommend diff simplification before commit. |
| **M3: The Promoter Stage** | `src/orac/promoter.py` | On goal completion: generate human-readable changelogs, reconcile `TODO.md` / `roadmap.md` checkboxes, and emit outbound digests via Slack/WhatsApp connectors. |
| **M4: Verifier-Watch Integration** | `src/orac/notify.py` | Combine reservoir/WIP advisories with promoter summaries to give the operator complete situational awareness. |

---

## 5. Summary Principle

The harness provides the structural rails:
1. **Frontier sets the pattern (Prewalk)**.
2. **Worker builds the slice**.
3. **Critic guards the floor**.
4. **Promoter finishes the job by making it visible**.
