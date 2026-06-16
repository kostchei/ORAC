# Engineering Spec — Group 2 Communications (channel.send), first slice

**Status:** Proposed · **Owner:** ORAC core · **Date:** 2026-06-16
**Decisions taken:** both channels (Slack + WhatsApp) behind one channel-agnostic tool; full
**draft → approve → send**.

## Framing (do not conflate two things)

- **Control plane (exists):** operating ORAC *from* WhatsApp/Slack — inbound, self-chat only
  ([chat_gateway.py](../src/orac/chat_gateway.py), the WhatsApp bridge, `chat_slack`). The agent
  gains **no** messaging power from it. See [chat-control-plane](chat-control-plane.md).
- **Group 2 Communications (this spec):** the **agent sending messages as work**, to arbitrary
  recipients. Same transport, different capability and governance. This is the first non-git,
  irreversible external action ORAC takes.

## Unblocked: the credential vault already exists

The roadmap lists the credential vault as the hard blocker for Group 2 and marks it `[ ]` — but it is
**built**: [credentials.py](../src/orac/credentials.py) (`CredentialStore`) seals secrets with
Windows DPAPI, stores opaque `credential_ref`s in `.orac/credentials.json`, and `redact()`s secrets
out of logs. `chat_slack` already reads tokens through it. So Group 2 is unblocked; the roadmap is
stale on this point (corrected there).

## Design

One channel-agnostic capability, two backends (Slack Web API; WhatsApp via the existing bridge
`/send`). Transports are injected so they are mockable and **fail closed** when a channel is not
configured — nothing sends for real until a `credential_ref` is present.

| Tool | Risk class | Behaviour |
| --- | --- | --- |
| `channel.read` | reversible · local → **auto** | Read recent messages for a channel/target. Read-only. |
| `channel.draft` | reversible · local → **auto** | Record the proposed message as a reviewable artifact (journaling). |
| `channel.send` | irreversible · external → **approve** | Park first; on approval, send via the backend. |

**draft → approve → send is the existing park/approve machinery — no new gate.** A `channel.send`
classified `approve` parks the request; the parked entry shows the **exact recipient + text** (that
*is* the draft the human reviews in the cockpit). `orac approve <id>` → the broker re-dispatches the
identical request → the adapter sends. `orac deny` blocks it. `channel.draft` is the optional compose
step that produces the text; the parked send is the authoritative review point.

**Credentials.** Backends fetch secrets via `CredentialStore(root).get(credential_ref)` (Slack bot
token; WhatsApp bridge session/url from `chat_config`). Never logged — `redact()` at the logging
layer. Absent credentials → the backend raises (fail closed), never a plaintext fallback.

**Rollback = human-in-the-loop correction (reuses the framework built this turn).** A sent message is
irreversible — there is no auto-undo. `channel.send` records a `RollbackContract` whose
`inverse_operation` is `channel.post_correction` (no auto-handler in `apply_rollback`), so
`orac rollback` lands on the **human-in-the-loop path**: "cannot auto-undo; send a correction to
<target>, then ack." This is exactly the case the rollback framework's non-automatable branch exists
for. (The local `fs.write_external_file` producer stays as the *auto*-reversible example; comms is
the *non-automatable* example.)

**One broker change required.** After an `APPROVE` action is approved and dispatches, the broker does
**not** currently record a notification ([broker.py:204](../src/orac/broker.py) notifies only on
`NOTIFY`/standing grants). Extend it to also record a notification for `APPROVE`-mode dispatches, so
the sent message + its rollback contract land in the review queue (auditable, and reachable by
`orac rollback`).

**Privilege separation (the one-writer invariant).** A new **Messenger** agent (slug `messenger`,
kind `doer`) is the *sole* holder of `channel.read`/`channel.draft`/`channel.send`. No reviewer,
orchestrator, or the Builder holds them — asserted by the existing §4.6 grant test, extended to
comms. No standing grants for `channel.send` initially (account-reputation risk).

## Files

- `src/orac/comms_adapters.py` (new) — channel-agnostic adapters + Slack/WhatsApp backends (injected).
- `src/orac/broker.py` — register comms adapters; record notification on `APPROVE` dispatch.
- `src/orac/policy.py` — classify the three tools; `channel.send` → approve.
- `src/orac/rollback_contract.py` — `channel.post_correction` is intentionally *not* auto-handled.
- `src/orac/prompts/agents.json`, `src/orac/tools/catalog.json` — Messenger agent + tool entries.
- `src/orac/work.py` — (slice 2) `comms` work-kind doer = `messenger` + a comms verifier
  ("send dispatched / backend returned a message id"); updates the test that asserts comms blocks for
  lack of a doer.

## Sequencing

1. **Slice 1 — capability + governance + rollback.** Adapters (read/draft/send) with mockable
   backends, classification, Messenger grant, the broker `APPROVE`-notification change, rollback as
   human-in-loop. Tests: send parks → approve → sends (fake transport) → notification carries the
   contract → `rollback` hits the human-in-loop correction path; `read`/`draft` are auto; an
   unconfigured channel fails closed.
2. **Slice 2 — agent-loop wiring.** `comms` work-kind doer = Messenger + verifier, so a comms *goal*
   actually runs the Messenger session. Updates `test_non_code_kind_without_doer_blocks_visibly`.

## Non-goals (this slice)

- No standing grants for sends. No bulk/mass send. No browser-automation messaging (official APIs
  only). No LinkedIn/Reddit/etc. — Slack + WhatsApp first.
