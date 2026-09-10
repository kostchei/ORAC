# Group 4 — Physical: implementation plan

## Context

Every other line item on TODO.md's "Next Up" list is now checked off (review cockpit
actions, Group 3 Media, operational durability, Group 5 Human Events). Group 4 —
Physical (device control via Home Assistant/MQTT) is the last unchecked item on both
TODO.md and [roadmap.md](roadmap.md), and it's the one remaining gap before ORAC's tool
surface matches its stated scope.

This is not a green-field design — the project already decided the policy and left
stubs:

- [tool-categories.md](tool-categories.md) §4 (Physical Devices) already mandates: automation
  hub, not per-device hacks; `read_state`/`prepare_action`/`execute_action` as separate
  capabilities per device; approval-by-default; e-stop as a distinct safety primitive;
  cooldowns + rate limits; log every action; never auto-discover/control arbitrary LAN
  devices.
- [compensating-actions.md](compensating-actions.md) already states the posture for physical:
  "approval-first, never infer an inverse; e-stop is a separate safety action, not
  rollback."
- `src/orac/work.py:113-121` already has a `"physical"` `WorkKindSpec` stub
  (`doer_slug=None`, contract rules naming the three-call order and approval/cooldown
  requirement).
- `src/orac/policy.py:139-152` already has real `_THROTTLE` entries for every
  `(Reversibility × Externality.PHYSICAL)` combination.
- `README.md` already names the target standing-grant UX: `orac standing add --agent
  Operator --tool execute_action --daily-cap 3 --reason "feed the fish"`.

So the job is to implement against an already-decided contract, following the exact
vertical-slice pattern used for Group 3 (Media) and Group 5 (Human Events): adapter →
store → risk classification → catalog entry → agent → work-kind verifier → rollback
hook → tests. Nothing here should re-litigate reversibility/approval policy that's
already settled in the docs above.

## Design decisions

- **Backend-first, mock always available.** Mirror `ComfyBackend` Protocol +
  `LocalMockComfyBackend` (`src/orac/media_adapters.py`): define a `PhysicalBackend`
  Protocol (`list_entities`, `read_state`, `call_service`) and a
  `LocalMockHomeAssistantBackend` that simulates a handful of fake entities in memory.
  Real Home Assistant integration (REST/WebSocket API) is a second backend
  implementing the same Protocol — build it, but the mock is what tests and the
  no-hardware dev loop run against.
- **New agent: `Operator`**, not Producer-reused. `work.py`'s existing stub comment
  ("Operator: sole holder of execute_action") already names it. Grants:
  `physical.read_state`, `physical.prepare_action`, `physical.execute_action`,
  `skill.list`, `skill.view` — no other agent gets `physical.execute_action`.
- **Three-call contract is enforced by state, not just convention.** `prepare_action`
  must persist a "prepared" record (device id, target state, captured pre-state,
  expiry) that `execute_action` requires and consumes; calling `execute_action` without
  a live `prepare_action` record is a hard error, not a policy nudge.
- **Allowlist is config, not code.** Devices must be explicitly enumerated (id, entity,
  backend, cooldown seconds) in a small config (e.g. `.orac/physical_devices.json` or a
  section of the existing config store) — `list_entities`/`read_state`/actions only
  work for allowlisted device ids. No auto-discovery call is exposed as a tool.
- **Cooldown is per-device, independent of the broker's daily-cap standing-grant
  mechanism.** `bump_rate`/`rate_count` (used by `_standing_grant_clears`,
  `broker.py:221-235`) is per agent+tool+day and stays as the standing-grant cap
  (reused for the fish-feeder example). Add a separate per-device last-action
  timestamp in the new physical store for sub-day cooldowns; `prepare_action` and
  `execute_action` both check and reject inside the cooldown window.
- **E-stop is a distinct tool, not a rollback.** `physical.emergency_stop` is its own
  adapter method, always available to Operator, bypasses the prepare/execute pairing,
  and is classified `(Reversibility.REVERSIBLE, Externality.LOCAL)` → AUTO (a safety
  brake must never itself be gated behind approval).
- **Risk classification** in `_ADAPTER_RISK` (`policy.py:48-108`, new "Physical (Group 4)"
  block):
  - `physical.list_entities`, `physical.read_state` → `(REVERSIBLE, LOCAL)` → AUTO
    (read-only, allowlist-scoped).
  - `physical.prepare_action` → `(REVERSIBLE, PHYSICAL)` → NOTIFY (captures pre-state,
    stages the call, does not touch the device).
  - `physical.execute_action` → `(IRREVERSIBLE, PHYSICAL)` → APPROVE (matches "approval
    by default"; a standing grant per `orac standing add` is the sanctioned bypass, same
    mechanism as Media/Comms already use).
  - `physical.emergency_stop` → `(REVERSIBLE, LOCAL)` → AUTO.
- **Rollback/compensation** (`compensating.py::execute_rollback`): `execute_action`
  attaches a `rollback_contract` pointing back at `physical.execute_action` with the
  captured pre-state as target args (only when the device is known to support a
  restoring call — e.g. toggling a switch back — never inferred). When no real inverse
  exists, the contract is omitted and the operator prompt says so explicitly, per
  compensating-actions.md's "no inverse... approval-first" posture. Add a
  `tool.startswith("physical.")` state-drift branch in `execute_rollback`
  (`compensating.py:118-138` is the Media analog) that calls `physical.read_state`
  before compensating, to confirm the device hasn't drifted since the contract was
  captured.

## Files

- `src/orac/physical_adapters.py` (new) — `PhysicalBackend` Protocol,
  `LocalMockHomeAssistantBackend`, `PhysicalAdapterSet` (methods: `list_entities`,
  `read_state`, `prepare_action`, `execute_action`, `emergency_stop`), and
  `physical_adapters_for(repo_root, backend=None, store=None)` factory — same shape as
  `media_adapters_for`.
- `src/orac/physical_store.py` (new) — allowlist load/validate, prepared-action records
  (with expiry), per-device cooldown timestamps, action audit log. SQLite under
  `.orac/physical.db`, mirroring `media_store.py`'s `MediaStore`.
- `src/orac/broker.py` — register `physical_adapters_for(repo_root)` in `_adapters`
  (`broker.py:118-137`), alongside the Media/Events registrations.
- `src/orac/policy.py` — new `_ADAPTER_RISK` block for the four `physical.*` tools (see
  above).
- `src/orac/tools/catalog.json` — four entries (`name`/`description`/`regular_use`/`inputs`),
  new "Physical (Group 4)" block after the Events block.
- `src/orac/prompts/agents.json` — new `Operator` agent block (`slug:"operator"`,
  `kind:"doer"`, `prompt_file:"operator.md"`, `protocol_file:"operator_max.json"`,
  scoped tool list above).
- `src/orac/prompts/operator.md`, `src/orac/prompts/operator_max.json` (new) — system
  prompt and protocol file, following the Producer/Host precedent files.
- `src/orac/work.py` — fill in the existing `"physical"` `WorkKindSpec` stub
  (`work.py:113-121`): set `doer_slug="operator"`, add
  `verifiers=("verify_physical_action",)`. Add `_verify_physical_action` (analog of
  `_verify_media_artifact`) that reads back device state via the physical
  store/adapter and confirms it matches the prepared target state.
- `src/orac/compensating.py` — add the `physical.*` state-drift branch described above.
- `src/orac/cli.py` — `orac physical list` / `orac physical read <device>` operator
  convenience commands (read-only; mirrors existing `orac board`/`orac event` CLI
  ergonomics), plus surfacing the allowlist/cooldown config path.
- Tests (new, mirroring `tests/test_media.py`'s structure): `tests/test_physical.py` —
  agent registration, risk classification for all four tools, allowlist enforcement
  (denied for a non-allowlisted device id), cooldown enforcement, full
  `read_state → prepare_action → execute_action` flow through the adapters dict with
  rollback_contract shape assertion, e-stop bypass, and the `physical` work-kind
  verifier.

## Verification

- `pytest tests/test_physical.py -q` plus the full suite (`pytest -q`) to confirm no
  regressions in the 469 existing tests.
- `python scripts/validate_governance_path.py` — extend it with a physical dispatch
  case (AUTO read_state, APPROVE execute_action, standing-grant fallback) the same way
  it already covers Comms/git/Efficiency, since TODO.md's "Prove the governance path"
  item established this as the cross-cutting smoke suite.
- Manual smoke: `orac physical list` against the mock backend with no real Home
  Assistant instance running, then drive one `prepare_action` → `execute_action` pair
  through the CLI/approval flow end-to-end to confirm the approval prompt, cooldown,
  and audit log all fire.
- Update TODO.md and [roadmap.md](roadmap.md) to check off Group 4 once the slice
  lands, same as the prior groups.
