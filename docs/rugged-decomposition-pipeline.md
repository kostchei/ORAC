# Rugged Decomposition Pipeline

**Status:** Partially implemented (deterministic floor wired; see §13)
**Purpose:** Turn large goals into small, verifiable execution slices that local
models can complete reliably — across code today and other tool surfaces later.

## 1. Why This Exists

Local models are useful *doers* but weak *owners* of large end-to-end problems.
In live testing with `qwen/qwen3-coder-next`, a whole-repo coding prompt produced
plausible code and self-tests yet still missed semantic requirements (e.g. "most
common values" as frequency counts), and strict structured output timed out on
large artifacts. Smaller slices with independent verification are the safer shape.

The answer is **not** "ask the model for a todo list." The answer is a rugged
execution pipe where every slice is a contract that cannot complete on the doer's
claim alone:

```text
intent lock -> decompose -> [deterministic floor] -> plan review -> dispatch
            -> execute slice -> verify slice -> return evidence -> integrate
```

## 2. Non-Negotiable Invariants

1. **No slice without a verifier.** If a slice cannot name how it will be checked,
   it is not executable work.
2. **No doer verifies its own done claim alone.** The doer returns evidence; ORAC
   verifies independently.
3. **No integration without evidence.** Parents accept only verified returns.
4. **One owner per mutable surface.** A slice has one doer and one bounded
   ownership area; two slices may not own the same resource without ordering.
5. **Generated tests are not enough.** The parent or verifier supplies at least
   one independent check where practical.
6. **Small-goal bypass stays valid.** Decomposition is a cost; trivial work stays
   single-doer.

## 3. Slice Contract

A slice is promoted from a loose subtask to an executable contract
(`decomposition.SliceContract`). Minimal fields:

```text
sub_intent  goal  work_kind  acceptance_criteria  inputs
allowed_tools  forbidden_tools  owned_paths_or_resources
verifier  risk_class  budget  expected_artifact  return_evidence  integration_note
```

The contract is self-contained: a doer executes it without the parent's full
thread history, and the verifier can reject a vague success claim.

## 4. Pipeline

### 4.1 Intent Lock

Intent freezes the real goal, constraints, must-include facts, and acceptance
criteria. Decomposition is invalid until this exists.

### 4.2 Decompose

The Orchestrator proposes slices only when the goal earns it
(`orchestrator.propose_decomposition`), under the honest *abundance frame* (it is
told the live free-subagent count to bias toward decomposing, self-tightening as
the roster fills). Output is preserved as full contracts, not flattened to
`{sub_intent, goal}`.

Good slices: implement one function + its test; update one doc section and verify
readback; draft one message but do not send; generate one media artifact and check
its metadata. Bad slices: "finish the feature", "clean up everything", "verify it
works" as doer-owned work, or two slices editing the same file with no ordering.

### 4.3 Deterministic Floor (then Plan Review)

Two gates, cheapest first:

- **Deterministic floor** (`decomposition.validate_decomposition`) — the things
  ORAC knows *without* model judgment: valid work kind, an available doer, a named
  verifier per slice drawn from the kind's allowed set, no overlapping ownership,
  no placeholder goals, parent still open, slice count within budget. A failure
  here blocks the parent with the reasons and **spends no model tokens**.
- **Plan review** (`plan_review.review_decomposition`) — the semantic question the
  floor can't answer, judged by three lenses: **Intent** (do the slices cover the
  goal with no gap/drift?), **Simple** (is this the minimal split?), **Efficiency**
  (any duplicate/overlapping/off-goal slice?). Aggregates like the council: any
  BLOCK → rejected; any ESCALATE → human.

### 4.4 Dispatch Gate

A slice dispatches only when **both** agree (`dispatch.both_agree`): the
Orchestrator proposed it (approved plan) **and** Optimise admits it (a free roster
slot and room in the resource band). A refused spawn defers the slice; it is not an
error.

### 4.5–4.8 Execute → Verify → Return → Integrate

The doer receives only the contract, makes one bounded change through the broker,
and runs the slice verifier. Verification is external to the doer's claim
(`work.verify_goal_done`); a failure creates a *focused repair slice* carrying the
failure output, never a broad retry. A slice returns only after verification; the
parent integrates verified evidence and reaches DONE only when the intent ledger
(`intent_ledger`) shows every declared slice satisfied.

## 5. Work-Kind Verifiers

Every doer-bearing work kind declares at least one verifier; a kind with a doer and
no verifier fails closed at spawn (`work.WORK_KINDS`).

| Work kind | Example verifier | Evidence |
| --- | --- | --- |
| `code` | `run_tests`, `verify_local_app`, hidden smoke | test output, screenshot/state |
| `comms` | draft-readback + human approval | draft id, no-send proof, approval |
| `media` | artifact metadata + visual QA | path, dimensions, preview |
| `physical` | approval + pre/post state read | device state before/after |
| `event` | session state-machine check | participant state, transcript |

## 6. Coding-Specific Pattern

```text
1. inspect existing shape      -> verifier: summary names exact files/functions
2. implement smallest core     -> verifier: focused test or smoke command
3. add edge-case handling      -> verifier: hidden / parent-supplied case
4. wire CLI/UI/API surface     -> verifier: command/browser smoke
5. integration cleanup         -> verifier: full suite + review lenses
```

Do not send "build the whole repo" to a local model unless the repo is genuinely
tiny and the verifier is cheap.

## 7. Future Tooling Pattern

The same contract/verify/return shape applies off-code: a document slice verifies
by export/readback of required clauses; a spreadsheet slice by recalculation over
exact ranges; a message slice by a draft that provably did **not** send pending
approval; a media slice by artifact dimensions/metadata; a physical slice by a
pre/post device-state read under an approval or standing grant.

## 8. Failure Modes To Guard

| Failure | Guard |
| --- | --- |
| Task confetti (too many tiny slices) | Simple/Optimise plan review + slice-count cap |
| Vague slices | deterministic contract validator |
| Generated tests miss the bug | parent-supplied / hidden checks |
| Overlapping edits | owned-path/resource check |
| Infinite repair loop | retry cap + BLOCKED state with evidence |
| Doer claims done early | mandatory verifier before return |
| External side effect without approval | risk model + broker approval |
| Parent forgets a slice | intent-ledger coverage gate |
| Local model stalls on strict output | plain completion + parser/repair |

## 9. Metrics (per parent)

slices proposed / admitted / rejected by review · verifier pass/fail · repair
slice count · wall time & tokens per slice · files changed per slice · acceptance
criteria covered · hidden-check failures. `score_decomposition` surfaces the
headline (`direct | decompose | reject`, slice count, estimated cost) as a log line.

## 10. Done State

1. A large code goal splits into verified slices automatically.
2. A verifier failure creates a focused repair slice, not a broad retry.
3. A parent cannot reach DONE with uncovered acceptance criteria.
4. At least one non-code work kind uses the same contract/verify/return shape.
5. The review cockpit shows slice evidence clearly enough to approve/deny/rollback.

## 11. One-Line Rule

Decomposition is only valuable when every slice is smaller than the original
problem, has one owner, has one verifier, and returns evidence the parent can
integrate.

## 12. Module Map

| Concern | Where |
| --- | --- |
| Slice contract + deterministic floor + scoring | `decomposition.py` |
| Abundance frame + proposal | `orchestrator.py` |
| Plan-review lenses (the counterweight) | `plan_review.py` |
| Both-agree dispatch gate | `dispatch.py` |
| Intent ledger (coverage gate) | `intent_ledger.py` |
| Work kinds, verifiers, fan-out runner | `work.py` (`run_orchestrated_goal`) |

## 13. Implementation Status

**Done in this branch (`claude/rugged-decomposition-pipeline`):**

- `decomposition.py` — `SliceContract`, `normalize_decomposition`,
  `validate_decomposition`, `score_decomposition` (the deterministic floor + the
  contract shape, §3 / §8 steps 1–3).
- `propose_decomposition` preserves full contracts and defaults each slice's
  verifier from the work kind (§4.2), instead of flattening to `{sub_intent, goal}`.
- `run_orchestrated_goal` runs the deterministic floor **before** the model
  plan-review (§4.3): a structurally broken plan blocks the parent and spends no
  model tokens; the `score_decomposition` recommendation is logged as telemetry.
- **Contract carried end-to-end.** The intent ledger (`intent_ledger._ledger_entry`)
  keeps the full contract per slice; `run_decomposed_goal` threads each slice's
  scope onto its child and its `inputs` into the child's context.
- **Broker enforces the contract at the edge** (`policy.contract_denial`, called in
  `broker._decide`): a doer is denied a tool its slice forbids / does not allow, or
  a write/commit outside its `owned_paths` — invariant #4 ("one owner per surface")
  as a runtime guarantee, not just a plan-time check. Empty/absent fields impose no
  restriction, so non-slice contracts pass through.
- **Bounded repair loop** (`run_goal_task(max_repairs=...)`, §4.5–4.8): a verifier
  failure re-runs the doer with the exact failure injected and a "fix only this"
  rule, up to N times, before blocking. Opt-in (default 0); the orchestrated fan-out
  turns it on (default 2).
- **Scrum routing** (`Scrum._should_decompose`): a goal earns the fan-out when it is
  estimated large (points > 1), spells out several steps (> 5 description lines), or
  sets an explicit `decompose` flag; otherwise one doer owns it. Structural signals,
  no string-sniffing.

**Closed (2026-06-15):**

- **Repair is a new focused slice**, not an in-place re-run. A verification failure
  spawns a contract-bounded, board-visible repair child of the failed slice, carrying
  the exact failure and inheriting its scope; it is independently verified, and a
  repair that itself fails chains one more bounded repair (`run_goal_task` recursion).
- **The per-slice RETURN edge gets a full council review** (`plan_review.review_return`)
  on top of the deterministic verifier: three lenses judge the returned work is
  on-goal, minimal, and waste-free before it integrates; a rejected return blocks the
  slice. Opt-in (`run_goal_task(review_return=...)`); the fan-out turns it on, on the
  local child brain.
- **Subagents recurse.** A slice flagged `decompose` fans out again via a nested
  `run_orchestrated_goal`, bounded by `max_depth` *and* the global roster cap (a full
  roster runs the slice as a single doer instead of nesting). The sub-fan-out plans on
  the foundation brain and runs its children on local.
