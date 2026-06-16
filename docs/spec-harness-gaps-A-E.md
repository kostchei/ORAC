# Engineering Spec — Harness Gaps A–E

**Status:** Proposed · **Owner:** ORAC core · **Date:** 2026-06-16
**Source:** [harness-gap-analysis.md](harness-gap-analysis.md) items A–E
**Governing rule (no fallbacks — throw):** every score, check, and gate below **fails closed**.
A missing, unparseable, or unrunnable result blocks or escalates; it never silently passes. This
matches the existing council contract (`council.py`, `plan_review.py`: an unparseable lens verdict
is `ESCALATE`, never a silent pass).

---

## 0. What already exists (so we extend, not duplicate)

Reading the code first changed the shape of A and B materially:

| Item | Already in tree | Gap to close |
| --- | --- | --- |
| A | `plan_review.review_return()` ([plan_review.py:186](../src/orac/plan_review.py)) runs Intent/Simple/Efficiency over returned work, aggregating block→denied / escalate→pending. | No **Security** lens; no **scalar quality score**; no **hard security floor**; off by default on the single-doer path. |
| B | `run_goal_task(..., max_repairs)` ([work.py:289](../src/orac/work.py)) spawns a **repair slice** carrying the failure detail when a *verifier* fails. | Repair is triggered **only** by verifier (tests/app) failure. A RETURN-review rejection or a low score just `BLOCK`s — no feedback-driven iterate. Repairs are off (`max_repairs=0`) on the single-doer path. |
| C | `AgentSession.run` ([agent_session.py:72](../src/orac/agent_session.py)) bounds a session by `max_steps`; the PROTOCOL *asks* the model not to repeat. | Nothing **enforces** non-repetition. A stuck model burns the step budget (and the 60% band) on identical calls. |
| D | `git.commit`/`git.push` record a sha; `git.revert` rolls back a pushed commit; `git.stash`/`stash_pop` exist ([code_adapters.py](../src/orac/code_adapters.py)). | Uncommitted working-tree edits (`repo.write_file`/`edit_file`) have **no independent checkpoint** — rollback needs a commit sha. |
| E | `audit`, `reviews`, `rate_counters` tables ([broker_store.py:22](../src/orac/broker_store.py)) hold the raw data; `orac reviews --json` exports it. | No **aggregation/trend** surface; nothing feeds back into lens calibration or decomposition sizing. |

The thread tying A+B+E together: the RETURN edge is where ORAC produces a **labelled quality
signal**, the repair loop is what **acts** on it, and metrics are what **learn** from it. They should
land together.

---

## A. Scalar quality + first-class Security lens on the RETURN edge

### Design

Extend `review_return` rather than replace it. Today it returns a `CouncilVerdict` (allowed/denied/
pending). We add:

1. **A fourth lens — Security.** Same persona-driven structured call as the other three, with its own
   focus prompt (auth on mutating paths, input validation, secrets in code/logs, SSRF/SQLi, CORS).
   A new persona `prompts/security.md` + slug `security` in `agents.json` (review-only; **no write
   grant**, like the other lenses — §4.6 invariant, asserted by the existing "no reviewer holds a
   write grant" test).
2. **A scalar score per lens.** Each RETURN lens additionally returns an integer `score` 1–10. The
   schema gains a required `score` field:
   ```python
   RETURN_SCORE_SCHEMA = {
     "type": "object",
     "properties": {
       "decision": {"type": "string", "enum": ["pass", "block", "escalate"]},
       "score": {"type": "integer", "minimum": 1, "maximum": 10},
       "reason": {"type": "string"},
     },
     "required": ["decision", "score", "reason"],
     "additionalProperties": False,
   }
   ```
   A reply missing/out-of-range `score` parses as `ESCALATE` (fail closed), exactly as a missing
   `decision` does today (`_parse`).
3. **Weighted aggregate + ship threshold.** Weights mirror the harness
   (Functionality/on-goal 30, Quality/Simple 25, Security 25, Edge/Efficiency 20 — mapped onto
   ORAC's lenses: Intent→functionality-on-goal, Simple→quality/shape, Efficiency→waste/edge,
   Security→security). Weighted total `< SHIP_THRESHOLD (7.0)` → the verdict is **at least**
   `PENDING` (escalate) even if every lens said "pass", carrying the score breakdown in the reason.
4. **Hard security floor.** If the Security lens reports an auth bypass / secret leak / injection
   (a dedicated `decision: "block"` from the Security lens, OR `security_score == 1`), the verdict is
   `DENIED` regardless of the weighted total. This is a deterministic post-rule over the lens
   verdicts, not a model judgment.

The aggregation precedence becomes: **security floor → any BLOCK → below-threshold or any ESCALATE →
allowed.**

### New type

`CouncilVerdict` is unchanged (status + lenses + reason). Per-lens `score` rides on `LensVerdict`
via a new optional field `score: int | None = None` (`models.py`). The weighted total goes in the
`reason` string and into the `reviews` table (see E) — no new top-level type required.

### Wiring

`run_goal_task` ([work.py:253](../src/orac/work.py)) already calls `review_return` when
`review_return=True`. Change: default it **on for `code`** (the kind whose returns are
security-relevant), and pass the new scored path. The orchestrated fan-out
(`run_orchestrated_goal`) already sets it.

### Files
- `src/orac/plan_review.py` — Security lens, scored schema, weighted aggregate, security floor.
- `src/orac/models.py` — `LensVerdict.score`.
- `src/orac/prompts/security.md`, `src/orac/prompts/agents.json` — new review-only persona.
- `src/orac/work.py` — enable scored RETURN review on the `code` path.

### Tests
- Security lens blocks on a planted hardcoded secret / unauthenticated mutation fixture.
- Weighted total `< 7.0` with all-pass lenses → PENDING (not allowed).
- `security_score == 1` overrides a high weighted total → DENIED.
- Unparseable / missing `score` → ESCALATE (fail closed).
- §4.6 invariant test extended: `security` slug holds no write grant.

---

## B. Bounded iterate-with-feedback (route review/score failure into the repair loop)

### Design

The repair machinery exists; it just isn't reachable from a RETURN-review rejection. Make the repair
trigger **any** of: verifier failure (today), RETURN-review `DENIED`/`PENDING`, or below-threshold
score. The repair slice already injects `context["verification_failure"]`; add a parallel
`context["review_feedback"]` carrying the per-lens reasons + score breakdown so the fresh Builder
session is told *exactly what to fix* (the harness's "feedback into a fresh Builder" move — and the
fresh session preserves the independent-reviewer-never-saw-builder-reasoning property).

Bound it the same way repairs are bound today: `max_repairs` (rename the mental model to
"max rounds", default **2** on the `code` path, matching the harness ≤3 total attempts). Each round is
a visible child slice on the board, independently verified and re-reviewed. Exhausting the rounds →
the slice `BLOCK`s with the final feedback, exactly as today.

This is strictly review-after: every round runs unattended; the operator sees the final state in the
review cockpit. No new approval gate.

### Wiring
- `work.py` `run_goal_task`: in the `verify_goal_done` ok-branch, when `review` is rejected, fall
  into the **same** repair path the verifier-failure branch uses (currently the rejection just
  blocks). Factor the repair-spawn into one helper called from both the verifier-fail and
  review-reject branches.
- Default `max_repairs=2` for `code`; keep `0` for kinds without verifiers.

### Files
- `src/orac/work.py` (the only behavioural change; ~1 helper + 2 call sites).

### Tests
- A goal that passes tests but the RETURN review rejects → a repair slice spawns with
  `review_feedback` in its context (not just verifier failures).
- Repair that then passes review → parent DONE via repair (existing assertion, new trigger).
- `max_repairs` rounds exhausted on persistent review rejection → BLOCKED with final feedback.
- Single-doer non-`code` kind (no verifier) still blocks on first failure (no repair).

---

## C. Tool-repetition / no-progress detector

### Design

Port Roo's `ToolRepetitionDetector` as a small deterministic guard inside `AgentSession`. Serialize
each decision to canonical JSON (tool + sorted args); count consecutive identical decisions; at
`limit` (default **3**) stop the session.

Two integration choices, pick the stricter-but-cheaper one:
- **In-session (chosen):** the detector lives in `AgentSession.run`; on trip it returns
  `SessionResult(status="blocked", summary="repetition limit: <tool> called N× identically")`. The
  existing `run_goal_task` flow then treats it like any block (and, with B, can trigger a repair
  round — the repair gets a fresh context, breaking the loop).
- Council-floor alternative (rejected for now): would require threading per-session call history into
  `ReviewContext`; heavier, and the loop is a *session* property, not an *edge* property.

Also catch **near-no-progress**: identical *observation* on a non-identical-but-same-tool call (e.g.
`repo.search` with trivially different args returning the same empty result) is out of scope for v1 —
exact-repeat is the 90% case and is cheap and unambiguous.

### Files
- `src/orac/agent_session.py` — a `_RepetitionDetector` dataclass + check before `broker.request`.

### Tests
- A brain scripted to emit the identical decision 3× → session blocks at step 3 with the repetition
  summary; broker is **not** called the 3rd time.
- A brain alternating between two tools → never trips (counter resets on change).
- Trip resets cleanly so a subsequent distinct call in a *new* session is unaffected.

### Why first
Smallest diff, no new types, no model calls, and it directly protects the unattended daemon's 60%
budget from a thrashing local model. Highest value-per-line.

---

## D. Shadow-repo per-task step checkpoints

### Design

Make every working-tree edit independently revertable, so "checkpoint-first / reversible" is literally
true at each step — the assumption that lets self-modification run `auto + notify`.

Approach (lighter than Roo's full shadow `.git`, fitted to ORAC's adapter): the `CodeAdapters`
([code_adapters.py](../src/orac/code_adapters.py)) gains a **per-task checkpoint** primitive built on
git that ORAC already shells out to:

- Before the **first** mutating call of a task on a root, record a checkpoint ref:
  `git stash create` (captures tracked + `-u` untracked into a dangling commit **without** touching
  the working tree) → store the resulting sha in a new `checkpoints` table keyed by `(task_id, root)`.
  Untracked-file capture matters because Builder writes new files.
- Expose two new Builder/operator capabilities:
  - `repo.checkpoint` — force a named checkpoint (returns sha). Risk class `auto` (read-like; writes
    nothing to the tree).
  - `repo.restore_checkpoint` — `git restore --source <sha> --worktree --staged .` + clean of
    untracked added since. Risk class: **notify** (reversible, local) — lands in the review queue
    like any restore.
- `rollback` ([the cockpit](../README.md)) gains a second target: an action with no commit sha but a
  recorded checkpoint can still be rolled back to that checkpoint. Today rollback "fails closed and
  asks you to undo it manually" when there's no sha — this closes that hole.

The checkpoint is created **through the broker** (so it's audited) but is itself non-mutating to the
tree, so it never trips the council.

### New table
```sql
CREATE TABLE IF NOT EXISTS checkpoints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    root        TEXT NOT NULL,
    sha         TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT ''
);
```

### Files
- `src/orac/code_adapters.py` — `checkpoint()` / `restore_checkpoint()` + auto-checkpoint on first
  write per (task, root).
- `src/orac/broker_store.py` — `checkpoints` table + `record_checkpoint` / `latest_checkpoint`.
- `src/orac/tools/catalog.json`, `agents.json` — `repo.checkpoint` (auto), `repo.restore_checkpoint`
  (notify); Builder-only write side.
- `src/orac/policy.py` — risk class for the two tools.
- rollback path (CLI/`notify.py`) — checkpoint as a rollback target.

### Tests
- First `repo.write_file` on a task auto-creates a checkpoint sha; second write does not duplicate it.
- `restore_checkpoint` after edits + a new untracked file returns the tree to the checkpoint exactly
  (the new file is removed, the edit reverted).
- `rollback` on an action with a checkpoint but no commit sha succeeds (regression on the
  fail-closed-without-sha path).
- Checkpoint/restore are audited; checkpoint does not trip the council (no tree mutation).

### Caveat
This is a guardrail against model mistakes, consistent with the broker's stated honesty — not a
security sandbox (design §8.2). `git stash create` is the cheapest capture that includes untracked
files without disturbing the tree; verify behaviour on the Windows shell path in a test.

---

## E. Metrics → self-tuning loop

### Design

A read-only aggregation over `audit` + `reviews` (+ the new RETURN scores from A and repair rounds
from B). No new write path on the hot loop — metrics are *derived*, computed on demand.

Per-task / per-day rollups (mirroring `metrics.md`, adapted to ORAC vocabulary):

| Metric | Derived from |
| --- | --- |
| Rounds-to-done | count of repair-slice descendants per goal (B) |
| First-round RETURN score | first `reviews` row of kind `return` per task (A) |
| Final RETURN score | last `return` review per task |
| Verification-failure rate | `verify_goal_done` failures / doer-claimed-done (audit/log) |
| Scope violations | count of F's scope-lens escalations (once F lands) |
| Escalation rate by lens | `reviews` grouped by `lens`,`decision` |
| Tool repetition trips | C's blocked-summary count |

Surface:
- `orac metrics` (CLI) — the rollup table; `--json` for machine consumption.
- `/api/metrics` (UI) — same data for the cockpit.

Feedback (the actual "self-tuning"):
1. **Lens calibration:** `orac lenses eval` (existing) consumes the labelled RETURN scores +
   human approve/deny outcomes from `reviews`/`pending_approvals` as additional curated cases.
2. **Decomposition sizing (feeds G):** observed rounds-to-done bucketed by goal size becomes the
   empirical prior the both-agree gate reads, replacing the hand heuristic in `_should_decompose`.

v1 ships the **read** surface (`orac metrics` + `/api/metrics`); the two feedback consumers are
fast-follows once a soak run has produced data — which is exactly roadmap build-order item 4.

### Files
- `src/orac/metrics.py` (new) — pure functions over `BrokerStore` queries; no mutation.
- `src/orac/broker_store.py` — a few `SELECT … GROUP BY` helpers (read-only).
- `src/orac/cli.py` — `orac metrics [--json]`.
- `src/orac/ui_server.py` — `/api/metrics`.

### Tests
- Seeded audit/reviews fixture → expected rollup numbers (deterministic).
- `--json` shape stable (consumed by lens-eval).
- Empty store → zeros, not an error.

---

## Sequencing & dependencies

```
C  (standalone, smallest, protects the daemon)        ── ship first
A  (scored Security RETURN edge)  ──┐
                                    ├─ land together: A produces the signal, B acts on it
B  (route review failure → repair) ─┘   (B depends on A's verdict/score)
D  (shadow checkpoints)            ── standalone; strengthens reversibility
E  (metrics read surface)          ── depends on A (scores) + B (rounds) existing in the tables
        └─► feeds G (empirical decomposition sizing), a fast-follow
```

Each step keeps the suite green (the project invariant). A, B, D, E each gate behind the `code` path
first; nothing changes for kinds without verifiers. No new approval gates are introduced — every
addition is review-after, consistent with the loop-never-blocks ruling.

## Implementation status (2026-06-16)

Landed, suite green (340 passed), each gated behind the `code` path / capability checks:

- **C — done.** `RepetitionDetector` in [agent_session.py](../src/orac/agent_session.py) stops a
  session on 3 identical consecutive tool calls *before* dispatch. Tests in `test_agency.py`.
- **A — done.** `review_return_scored` in [plan_review.py](../src/orac/plan_review.py): 4 lenses
  (Intent/Simple/**Security**/Efficiency), 1–10 score each, weighted total vs `SHIP_THRESHOLD=7.0`,
  hard security floor (Security block or score 1 → DENIED). Security persona is **inline** (no
  `agents.json` change, so it holds no grant by construction). `LensVerdict.score` added. Kept the
  existing 3-lens `review_return` intact for the orchestrated path. Tests in `test_plan_review.py`.
- **B — done.** `run_goal_task` routes a RETURN-review rejection into the existing repair loop
  (`max_repairs`), carrying `review_feedback` into the fresh repair slice. Scrum enables
  `scored_return=True` + `max_repairs=2` on the single-doer `code` path **only when the session brain
  supports `think_json`** (a visible capability gate, not a silent fallback). Test in
  `test_decomposition_runtime.py`.
- **D — core done.** `repo.checkpoint` / `repo.restore_checkpoint` in
  [code_adapters.py](../src/orac/code_adapters.py): a temp-index `write-tree` + `commit-tree`
  snapshot (captures untracked files, no shell, Windows-verified) and an exact diff-based restore
  (reverts edits, restores deletions, removes since-added files). Classified `auto`, granted to the
  Builder, in the catalog. Tests in `test_checkpoint.py`.
  **Deferred:** the `checkpoints` persistence table, auto-checkpoint-on-first-write, and the
  cockpit-`rollback`-to-checkpoint integration. The primitive exists; the durable wiring is the
  follow-up.
- **E — read surface done.** [metrics.py](../src/orac/metrics.py) + `orac metrics [--json]`: rollups
  over `audit` (by status/tool), `reviews` (escalations/blocks by lens), and queue depth. RETURN
  reviews are now persisted to the `reviews` table so they count. Tests in `test_metrics.py`.
  **Deferred:** a `score` column on `reviews` (RETURN scores are not yet stored per-lens),
  board-derived rounds-to-done, and `/api/metrics` for the UI. The two feedback *consumers* (lens
  calibration ingesting the data; empirical decomposition sizing for gap G) are fast-follows once a
  soak run produces data.

## Roadmap mapping
- A + B = the roadmap's "**promote the RETURN edge to a full council review**" (decomposition
  fan-out, "Next for this subsystem"), now with security and a ship threshold.
- E = the data surface roadmap **build-order item 4** ("soak run generates labelled escalation
  data") assumes but never specified.
- C, D = governance-spine hardening, foundation-before-breadth.
