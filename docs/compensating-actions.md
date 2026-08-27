# Compensating Actions

ORAC's `rollback` command must describe an honest inverse, not merely a second
action that might make the situation look similar. Git has a strong inverse:
`git.revert` records a new commit that undoes a known commit. Other tools need
an explicit compensation contract before they can use review-after.

## Contract carried by a completed action

A mutating adapter that can be compensated returns this object in its result
data, which is then preserved verbatim in the durable notification:

```json
{
  "rollback_contract": {
    "version": 1,
    "tool": "media.archive_asset",
    "args": {"asset_id": "asset-123"},
    "expected_state": {"review_state": "generated"},
    "expires_at": null,
    "operator_prompt": "Archive generated asset asset-123?"
  }
}
```

The broker may offer one-step rollback only when all of the following hold:

1. The originating adapter declared the contract after the action succeeded.
2. The compensation tool is registered, risk-classified, and allow-listed for
   the human principal; notification data cannot introduce an arbitrary tool.
3. Required identity and pre-state fields are present and schema-valid.
4. `expected_state` still matches. Drift fails closed and asks for manual
   reconciliation instead of applying a stale inverse.
5. The contract has not expired. A missing expiry means the adapter asserts the
   inverse remains meaningful indefinitely.
6. The compensation request passes the normal risk throttle. A physical,
   financial, public, or otherwise irreversible compensation is approval-first
   even though a human initiated the rollback.

Every attempt is written to the broker audit log with principal `human`, the
source notification id, the compensation request, the precondition result, and
the final outcome. The source notification is acknowledged only after the
compensation succeeds. A failure leaves it open.

## Tool-family rules

| Original action | Compensation | Required evidence | Handling |
| --- | --- | --- | --- |
| `git.push` | `git.revert`, optionally followed by `git.push` | repo root, commit SHA, branch, remote | Existing one-step rollback; fail if SHA is absent. |
| Local draft or generated asset | snapshot restore or `media.archive_asset` | immutable asset id, content digest, current review state | Local and reversible; state drift fails closed. |
| Media publish | provider-specific unpublish/delete | provider, remote object id, account ref, publish receipt, supported undo window | Hard external action. Approve the compensation; if the provider has no reliable delete API, classify publish as irreversible and approval-first. |
| Message send | none | send receipt retained for audit only | A correction is a new message, not a rollback, and requires its own approval. |
| Home Assistant/MQTT action | provider-specific restore of captured pre-state, when the device explicitly supports it | entity id, exact pre-state, observed post-state, freshness deadline | Physical approval-first. Never infer an inverse. E-stop is a separate safety action, not rollback. |
| Financial action | provider-native void/refund only | transaction id, settlement state, amount, currency, provider receipt, undo deadline | Financial approval-first. No generic inverse and no silent retry. |
| Human event transition | append a compensating transition | session id, prior state/version, participant-visible side effects | Local state may be reversible; already broadcast or acted-on effects remain explicit and may require a new approved communication. |

## Operator surface

Review entries must say one of:

- `rollback available` with the exact compensation tool and a concise prompt;
- `rollback expired` with the expiry and manual recovery guidance; or
- `no automatic rollback` with the reason the original action was
  approval-first.

Before execution, show the action being compensated, the captured pre-state,
the currently observed state, external effects, and whether the compensation
itself needs approval. Do not use a generic “Are you sure?” prompt.

## Admission rule for new mutating adapters

A new adapter cannot be classified as review-after merely because its output is
locally cached. Its registration must choose and test exactly one posture:

- return a validated compensation contract and prove successful, stale-state,
  expired, and failed-compensation paths; or
- declare no inverse and use approval-first for consequential external effects.

This is the gate for Media and Physical adapters. The generic contract resolver
and the first Media adapter should land together so the schema is exercised by
a real non-git tool rather than becoming unused framework code.
