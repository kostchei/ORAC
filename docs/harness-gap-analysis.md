# Harness Gap Analysis — what OpenClaw Harness and Roo-Code have that ORAC doesn't

**Status:** Analysis · **Owner:** ORAC core · **Date:** 2026-06-16

Two reference harnesses were read end-to-end and compared against ORAC's foundation
(see [edge-check-council-design.md](edge-check-council-design.md), [roadmap.md](roadmap.md),
[tool-categories.md](tool-categories.md)). The goal: find machinery these harnesses ship that
would help ORAC. The strongest finds are not new directions — they **concretize items already on
ORAC's own roadmap** (the RETURN-edge promotion, the soak-run metrics loop) rather than contradict
its philosophy.

ORAC already defines itself against both: the design doc notes it "does not inherit OpenClaw's
flat-hierarchy limitation" and cites "the Roo/OpenClaw lesson: instruction-in, summary-out." So the
headline ideas are absorbed. This doc is about what's *underneath* them.

> **Global rule that governs every item below (no fallbacks — throw an error):** any scoring,
> iterate, or scope addition must **fail closed**. A missing, unparseable, or unrunnable
> score/check blocks; it never silently passes.

---

## 1. The two harnesses

**openclaw-harness** ([guixiang123124/openclaw-harness](https://github.com/guixiang123124/openclaw-harness))
is a thin orchestration layer: a Lead agent drives a 5-phase pipeline
**SCOUT → BUILD → REVIEW → ITERATE → SHIP**. Builders spawn via ACP; an **independent Reviewer that
never sees the Builder's reasoning** scores the diff. It is a simpler, linear cousin of ORAC — but
it has three things ORAC doesn't (scalar scoring, an iterate loop, a metrics feedback loop).

**Roo-Code** ([RooCodeInc/Roo-Code](https://github.com/RooCodeInc/Roo-Code)) is a mature
single-process coding agent: **modes** (Architect / Code / Debug / Orchestrator / Ask) with per-mode
tool groups + **`fileRegex` restrictions**, boomerang `new_task` delegation, **shadow-git
checkpoints**, **context condensing**, a **tool-repetition detector**, and a codebase semantic index.

Local copies were cloned to `D:\Code\_harness_ref\` (outside the repo, untracked).

---

## 2. What ORAC is missing — ranked by fit

### High value — fills a real gap

#### A. Scalar quality + a first-class Security dimension on the RETURN edge

ORAC's council is a binary veto (`PASS`/`BLOCK`/`ESCALATE`) and the RETURN edge today is the
deterministic `verify_goal_done` floor — the roadmap explicitly wants to "promote the RETURN edge to
a full council review." The harness shows *what that review's content should be*: a **4-dimension
weighted score** (Functionality 30 / Code Quality 25 / Security 25 / Edge Cases 20) with a **numeric
ship threshold (≥ 7.0)** and a **hard security FAIL floor** (auth bypass / secret leak / SQL
injection ⇒ automatic fail regardless of other scores). See
`_harness_ref/openclaw-harness/agents/reviewer.md`.

ORAC has **no Security lens** and no notion of "good enough to ship." For a system whose whole
roadmap leads to comms / physical surfaces, a security dimension belongs in the council floor now —
not deferred to Group 2.

#### B. Bounded iterate-with-feedback, not dead-end BLOCK

Today a failed verifier → task `BLOCKED`. The harness loops BUILD → REVIEW → ITERATE (max 3 rounds),
feeding the reviewer's *specific* feedback into a fresh Builder session. This fits review-after
perfectly — the operator sees the final result — and turns a dead-end into a self-correcting loop.
The **independent-reviewer-never-sees-Builder-reasoning** rule is an anti-bias trick worth copying
directly into ORAC's RETURN edge.

#### C. Tool-repetition / no-progress detector

Roo's `ToolRepetitionDetector` halts the model after N identical consecutive tool calls. ORAC bounds
sessions by `max_steps` (`agent_session.py`) but has **no loop detection** — a stuck local model can
burn the entire 60% budget thrashing on the same call. Cheap, deterministic, and belongs in the
council floor / `agent_session.py`. Highest value-per-line of anything here, and it directly protects
the unattended daemon.

#### D. Shadow-repo step checkpoints

ORAC's whole risk model rests on "reversible-via-checkpoint," but `rollback` only works when an action
recorded a commit sha (`git.push`) — uncommitted working-tree edits from `repo.write_file` /
`repo.edit_file` aren't independently revertable. Roo's `RepoPerTaskCheckpointService` gives a shadow
git repo per task so **every step** is undoable. This makes "checkpoint-first" literally true at each
edit — the load-bearing assumption that lets self-modification run `auto + notify` instead of blocking.

#### E. Metrics → self-tuning loop

Roadmap build-order item 4 is "soak run generates labelled escalation data to tune the lens-eval
suite." The harness's `metrics.md` is the concrete surface: per-sprint rounds-to-pass, first-round
score, post-deploy bugs, scope violations → trend analysis → improved templates / prompts. ORAC has
the raw data (`audit` / `reviews` tables) but **no aggregation** that feeds back into lens
calibration or decomposition sizing. This is the recursive-self-improvement evidence loop, made
explicit.

### Medium value

#### F. Per-task declared file-scope enforcement

Both harnesses hard-enforce "only modify declared files" (Roo via `fileRegex` per mode — Architect
can edit only `*.md`; the harness via SPRINT.md scope + a scope-violation metric). ORAC enforces
**approved roots** (`code_adapters.py`) and carries `scope_constraints` in the `SubtaskContract`, but
doesn't gate the declared file list at the broker. A scope violation is a cheap, strong drift signal
— and a natural new deterministic lens.

#### G. Empirical decomposition sizing priors

The harness's sprint-sizing guide ("full-stack feature: < 20% success — always split") grounds
decomposition in observed success-rate-by-size. ORAC's `_should_decompose` is a hand heuristic; once
(E) exists, the both-agree dispatch gate can use real priors instead.

#### H. Path denylist within roots

Roo's `.rooignore` keeps the agent out of files even inside an allowed root. Pairs with the planned
credential vault — keep the Builder out of `.env` / secrets even when they live under an approved root.

### Lower value / already covered / off-philosophy

- **Context condensing** (Roo summarizes long sessions): ORAC's fresh-context-per-subtask +
  `max_steps` + decomposition is a deliberate *different* answer (Karpathy "keep agents on a leash").
  Worth noting for small local context windows, not urgent.
- **Semantic codebase index** (Roo): nice for self-improvement on a growing codebase; ORAC has
  ripgrep `repo.search`. Defer.
- **Modes / `update_todo_list`**: covered by `agents.json` + `work_kinds` + the task board.

---

## 3. Suggested sequencing against the existing roadmap

These slot into the foundation work, not the surface groups:

1. **C (repetition detector)** — smallest, protects the daemon; do first.
2. **A + B (scored RETURN edge + iterate loop)** — together they *are* the roadmap's "promote the
   RETURN edge to a full council review," and they generate the labelled data item 4 wants.
3. **D (shadow checkpoints)** — hardens the reversibility guarantee the risk model depends on.
4. **E (metrics loop)** — consumes the data from A/B/F; feeds G.
5. **F, G, H** — incremental hardening once the above are in.
