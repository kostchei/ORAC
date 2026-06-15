# The Council Contract

Operator-facing spec of ORAC's edge-check council: every lens, what it checks,
what its verdict means, and exactly which outcomes **deny**, **park**, **notify**,
or **pass**. This is the contract the broker enforces on every tool call; the code
of record is `council.py` (deterministic floor), `lenses.py` (LLM cognition layer),
`policy.py` (risk model), and `broker.py::_decide` (the pipeline that ties them
together).

## 1. The one-paragraph version

Every tool call an agent makes is adjudicated at a single choke point
(`ToolBroker._decide`). It runs a fixed pipeline: admission → slice-contract scope
→ **council** → risk model → standing grant → dispatch. The council is a panel of
**lenses**, each returning one of three verdicts — `pass`, `escalate`, `block`.
The verdicts aggregate by veto, not by vote: **any `block` denies the call; else
any `escalate` parks it for a human; else it passes** to the risk model, which
decides whether the (allowed) call runs silently, runs-and-notifies, or waits for
approval. The guiding principle is **review-after, not ask-before**: code work is
not blocked waiting for a human — it runs and lands in a review queue with a
one-step rollback — and `approve` is reserved for the genuinely irreversible.

## 2. Verdict vocabulary

Each lens returns a `LensVerdict` with one decision:

| Verdict | Meaning | Council effect |
| --- | --- | --- |
| **`pass`** | No problem in this lens's domain. | Contributes nothing; the call proceeds unless another lens objects. |
| **`escalate`** | A real problem a human should weigh. | If no lens blocks, the call is **parked** (`PENDING`) until a human approves the *exact* request. |
| **`block`** | This call must not run. | The call is **denied** immediately with the lens's reason. |

A verdict must agree with its reason: a lens that can *name* what is wrong must not
`pass`. "I cannot judge this" (from the LLM layer) is itself an `escalate`, never a
silent pass.

**Aggregation (veto, not vote):** `any block → DENIED` · `else any escalate →
PENDING` · `else → ALLOWED`. A veto is a stop with a recorded reason, not a score
to be averaged. Every non-clean review is persisted to the `reviews` table and
surfaced in `orac reviews`.

## 3. The deterministic floor (always on)

Five lenses run on **every** store-backed call. They are cheap SQL/state checks, so
they convene unconditionally — the floor never goes away, even when the LLM layer
is off. With no store (manifest/no-DB brokers) the state-backed lenses degrade to
`pass`; Intent and Sentinel still apply (they need only the task/args).

| Lens | Role | Checks | On trip |
| --- | --- | --- | --- |
| **Intent** | Goal drift | The task is not already `DONE`/`BLOCKED`. Acting on a closed task serves no locked goal. | **`block`** |
| **Optimise** | Fair share | The agent has used this tool fewer than the daily rate cap (default 200/day). A runaway loop burning the band is throttled. | **`escalate`** |
| **Simple** | Rebuild-or-keep | This tool has been used on this task fewer than the repeat threshold (default 30). Hammering one tool suggests patch-churn — the shape should be reviewed, not patched again. | **`escalate`** |
| **Efficiency** | Duplicate work | For `repo.write_file`, no *identical* (same args) write already succeeded on this task. | **`block`** |
| **Sentinel** | Self-modification guard | The write/commit does not touch a safety-critical file (`broker.py`, `policy.py`, `council.py`, `lenses.py`, `scrum.py`, `daemon.py`, `agent_session.py`, the grant seed `prompts/agents.json`, …). | **`escalate`** (even for the Builder, regardless of reversibility) |

Sentinel is the load-bearing safety lens: the system must not weaken its own brakes
or widen its own privileges under auto+notify. A human approval of the exact request
clears it (the durable approval short-circuits the re-issued call), so it escalates
without trapping the work forever.

## 4. The cognition layer (LLM lenses, P5)

Three of the lenses — **Optimise**, **Simple**, **Efficiency** — additionally
*reason* over an edge using a small local model, but only on **consequential
edges**: state-changing tools (`repo.write_file`, `repo.edit_file`, `git.commit`,
`git.push`, `git.revert`). Cheap edges (reads, status) stay deterministic — a
handful of model calls per build, not three per file read.

Each LLM lens reads the edge through its persona (`prompts/<slug>.md`) and returns
the same `pass`/`escalate`/`block` contract. Its verdict aggregates with the
deterministic floor unchanged: the floor is the guarantee, the model is the
judgment on top. A misconfigured cognition layer (a lens with no structured-output
brain) is a loud failure, never a silent wave-through. The personas:

- **Optimise** — resource governor; aims to spend ≤60% before escalating scope.
- **Simple** — fewest-components path; removes accidental complexity.
- **Efficiency** — waste / dead code / unverifiable-change hunter.

(**Intent** and **Sentinel** have no LLM layer — they are pure deterministic gates.)

## 5. From verdict to outcome: the full pipeline

`ToolBroker._decide` runs these gates in order. The first that fires decides:

1. **Admission** — unknown tool → `ERROR`; tool not in the agent's grant → `DENIED`.
   (The one-writer invariant lives here: only the Builder holds write grants.)
2. **Slice-contract scope** — a doer running a decomposed slice may use only the
   tools and touch only the paths its contract allows → `DENIED` if out of scope.
3. **Council** (§2–4) — `block` → `DENIED`; `escalate` → **parked** (`PENDING`)
   pending human approval of the exact request.
4. **Risk model** (§6) — for an allowed call, decide auto / notify / approve.
5. **Standing grant** — a pre-authorised `(agent, tool)` (optionally args-pinned,
   rate-capped/day) short-circuits an `approve` *park* — but **never** bypasses a
   council `escalate`: the safety floor is never waived by a standing grant.
6. **Dispatch** — run the call, bump the rate counter, and (for `notify` or a
   standing-granted action) record a notification for retrospective review.

## 6. The risk model: what an *allowed* call does next

The council decides *whether* a call is permitted; the risk model decides *how* a
permitted call is handled, from a total `(reversibility × externality)` throttle
table (`policy._THROTTLE`):

| Mode | Behaviour | Example |
| --- | --- | --- |
| **`auto`** | Run immediately, audit only. | Reversible + local: `repo.write_file`, `git.commit`. |
| **`notify`** | Run immediately, then queue the completed action for review ("I did X — ok? rollback available"). | `git.push` (hard-to-reverse but private). |
| **`approve`** | Park as `PENDING` until a human approves first. | Irreversible / financial / public-external — comms, money, physical. |

**Review-after, not ask-before:** code work never blocks on a human. `git.push`
*notifies* (runs + lands in the queue); `git.revert` is the one-step rollback;
`approve` is reserved for the genuinely irreversible. An unclassified tool fails
closed (raises) rather than defaulting to permissive.

## 7. Where outcomes surface

- **`block` / denied** — returned to the agent as a denied `CapabilityResult`; the
  agent adapts (a denial is an observation, not a crash). Recorded in `reviews`.
- **`escalate` / parked** — the task enters `PENDING_APPROVAL`; the loop parks it
  and resumes on the next tick once resolved. Surfaced in `orac reviews`; cleared
  by `orac approve <id>` / `orac deny <id>`.
- **`notify` / completed-and-queued** — the action already ran; it appears in the
  review queue. `orac ack <id>` accepts it; `orac rollback <id> [--push]`
  git-reverts the recorded commit under the `human` principal.
- **`pass` / allowed-auto** — runs silently; visible only in the audit trail.

The daemon prints the review-queue summary each tick when non-empty, and the UI
mirrors it at `/api/reviews`, so an unattended run surfaces its queue instead of
waiting to be polled.

## 8. Tuning knobs

| Knob | Default | Effect |
| --- | --- | --- |
| `Council.daily_rate_cap` | 200 | Optimise fair-share band (per agent, per tool, per day). |
| `Council.repeat_threshold` | 30 | Simple patch-churn threshold (per tool, per task). |
| `DUPLICATE_CHECKED_TOOLS` | `{repo.write_file}` | Tools whose exact repetition Efficiency blocks. |
| `LLM_REVIEWED_TOOLS` | write/edit/commit/push/revert | Edges the cognition layer reasons over. |
| `SAFETY_CRITICAL_PATHS` | governor + grant-seed files | What Sentinel guards. |

Defaults are generous on purpose: brakes against runaway loops, not friction for
normal work.
