# Runtime triage — the June board stall (examined 2026-07-29)

The persisted board carried 25 blocked tasks, 1 ready task, and 2 "active" subagent
reservations, all frozen at 2026-06-16. This doc records what actually caused that stall and
what to change. It is a **diagnosis, not a changelog** — none of the fixes below are
implemented yet.

The headline: **none of the 25 blocks were caused by the local model reasoning badly.** Every
one traces to a harness defect — truncated observations, a fallback that fabricates failure
text, or an escalation triggered by an outage. The board's own failure summaries are
misleading, which is why this needed an audit-log read rather than a work-log read.

**State examined:** `.orac/board.json` (revision 25, updated 2026-06-16T15:11Z),
`.orac/broker.db` (343 audit rows, 38 subagent reservations). LM Studio was down at
examination time (`http://localhost:1234/v1/models` → no response); no daemon was running.

---

## 1. Step-budget exhaustion (11 tasks) — silent observation truncation

The dominant failure, recorded as `Session blocked after 16 step(s): Step budget exhausted (16)
without done/blocked.`

`repo.read_file` returns the entire file body in `result.data`
([`code_adapters.py`](../src/orac/code_adapters.py) `read_file`). The session then JSON-dumps
that data and truncates it to `OBSERVATION_LIMIT = 1500` chars
([`agent_session.py`](../src/orac/agent_session.py) — the `OBSERVATION` append). There is no
truncation marker, and `repo.read_file` has no `offset` argument, so **the remainder of a large
file is unreachable by any sequence of calls**.

A doer asking for `src/orac/storage.py` (15,921 chars) receives roughly 1,400 chars of head and
no signal that anything was withheld. It does not have what it needs, so it asks again, gets
the identical truncated head, and repeats until the budget dies.

The audit log shows exactly this:

| Repeated call | Times in one session |
| --- | --- |
| `repo.read_file src/orac/audio_io.py` | 13 |
| `repo.search "StaleBoardError"` | 12 |
| `repo.read_file src/orac/storage.py` | 11 |

**164 of 343 audited calls (48%) were byte-identical repeats** across 44 distinct repeated
calls. Nine sessions terminated at exactly 16 steps.

`repo.search` fails the same way from the other end: it caps at `SEARCH_RESULT_LIMIT` (200)
matches, then the same 1500-char truncation leaves roughly ten visible with no total and no
indication more exist. A doer that searches `"def "` gets 200 matches, sees ten, and learns
nothing. Whole-history tool distribution tells the story: 159 `repo.search`, 119
`repo.read_file`, 22 `repo.write_file`, 7 `repo.run_tests`, 1 `repo.edit_file`, 1 `git.commit`.
The doers spent their lives orienting and almost never produced anything.

A repetition guard emitting `Repetition limit (3): repo.search called identically without
progress at step 7` appears in one board work log and in a stale
`__pycache__/agent_session.cpython-311.pyc`, but **exists nowhere in git history**. It was
written on a build branch and lost — the same class of loss recorded in the memory note about
the daemon switching branches under a live checkout.

## 2. "Unparseable model reply" (7 tasks) — the model never spoke

Recorded as `Session blocked after N step(s): Unparseable model reply at step N: "Builder
considered '<task title>' and recorded progress."`

That sentence is verbatim `RulesBrain.think`'s default return in
[`llm.py`](../src/orac/llm.py). It is not a model reply at all. `FallbackBrain` substitutes the
rules stub whenever the primary brain raises **or returns empty**, and the stub's prose then
fails `parse_decision`.

When LM Studio is up, the doer path uses `think_json` with `DECISION_SCHEMA` and LM Studio
enforces the schema at the token level — a malformed decision is physically impossible. So an
unparseable reply on the local path is not evidence of a weak model; it is evidence the local
model was **never reached**. These seven tasks record a fabricated model-competence failure in
place of "LM Studio unreachable".

This is the failure mode the project's no-fallbacks rule exists to prevent: the fallback
converted an infrastructure outage into a misleading verdict about model quality, and did it
silently enough to survive a month on the board.

## 3. Refusals (2 tasks) — browser escalation triggered by #2

Recorded as `The prompt describes a fictional agent architecture ('Builder', 'ORAC') and demands
interaction through a strict JSON protocol...` — a consumer chat UI declining to roleplay a tool
protocol. One sibling log reads `Local failed twice; escalating to browser (provider=gemini)`.

The "local failure" that triggered the escalation was the outage in #2. Browser brains cannot
enforce a schema — `BrowserFoundationBrain.think_json`
([`browser_brain.py`](../src/orac/browser_brain.py)) appends the schema as plain text and
relies on the caller's strict parser — so the refusal risk is real, but it should never have
been reached on a transport error.

**Contributing to both #2 and #3:** `OpenAICompatibleBrain._messages` hardcodes the system
message *"You are {agent}, the {role} agent in ORAC. Write only the concise work-log entry"* on
**every** call, including agent-session calls whose user turn says *"reply with a single JSON
object and nothing else"*. The doer's real persona (`prompts/builder.md`) is buried in the user
turn. LM Studio's server-side schema enforcement papers over the contradiction; any brain
without it obeys the system message and writes prose, or balks at the mismatch.

## 4. Not a problem — the two "active" subagents

Reservations 34 and 38 (2026-06-15 and 2026-06-16) are orphaned leases from a hard kill
mid-session; the last audit row (15:14Z) is three minutes after the board's final write.
`reap_stale_subagents()` runs on the next dispatch with a ten-minute window
([`dispatch.py`](../src/orac/dispatch.py) `optimise_admits`), and `MAX_SUBAGENTS` is 500. They
retire themselves on the next run and were never blocking anything. No fix needed.

---

## What to change, in order

Ordered so that each step's evidence is trustworthy before the next one uses it. The framing
throughout is local-first: the local model is not the bottleneck, the harness around it is.

**P0 — stop converting outages into fabricated model failures.** Remove `RulesBrain` from the
doer path (keep it for tests). An unreachable LM Studio must raise and block the task with the
real reason — *"LM Studio unreachable at localhost:1234"* — and an empty local reply is an
error, not a fallback trigger. Gate browser escalation on genuine reasoning failure only; a
transport error should retry local, never spend a browser turn. Without this, every measurement
taken after it is untrustworthy.

**P1 — make the local model able to succeed.** This is where local-first is won or lost. A
24–35B local model can drive this loop; it cannot drive it blind.

- Add `offset`/`limit` to `repo.read_file`, returning line-numbered slices and an explicit
  *"showing lines 1–200 of 412"*.
- Never truncate an observation silently — always append `…[truncated N chars; call with
  offset=X for the rest]`.
- Make `repo.search` legible to a small model: per-file counts, `path:line` hits, an honest
  total, and pushback on pathological queries rather than 200 unusable matches.
- Seed the first prompt with a cheap deterministic repo map (top-level tree plus files
  name-matching the goal) instead of making the doer discover the repo by search. On the
  observed transcripts this alone should reclaim 5–8 of the 16 steps.

**P2 — bound the loop honestly.** Re-add the lost repetition guard: an identical `tool` + `args`
call returns an observation saying so and naming what to vary; the third strike blocks with a
real reason. This converts a 16-step burn into a 3-step failure that names its cause. Only
*after* P1 should `max_steps` rise from 16 toward ~30 — raising it first just buys a longer
loop. Record budget-exhaustion separately from reasoned blocks so `self_tune` and `metrics` can
tell them apart.

**P3 — fix the prompt split.** Put `profile.system_prompt` in the system message for session
calls and drop the "write only the work-log entry" instruction there; keep that instruction for
the council's narrative lens calls, which genuinely want prose.

**P4 — then reconcile the board.** Don't hand-clear it; it is the test corpus.

- The 7 tasks blocked with `Builder considered …` are **false blocks** — never attempted by a
  model. Reset them to ready once P0 lands.
- Re-run a sample of the 11 budget-exhausted tasks as live-fire verification of P1 and P2.
- The single ready task `9e655c99` (a read-only diagnostic that classifies blocked tasks by
  blocker reason) is a good first canary: it is non-mutating, and it is precisely the tool that
  would have made this manual triage unnecessary.

This ordering matches the standing exit criterion in [`roadmap.md`](roadmap.md) build-order item
4 — reconcile state, then run a supervised canary — with the correction that the canary's
listed failure signals (*malformed model replies, step-budget exhaustion*) are not model-quality
signals at all until P0–P2 land.
