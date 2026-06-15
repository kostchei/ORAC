# Chat Control Plane - task control from WhatsApp & Slack

**Status:** Built in v1. **Decisions locked:** both channels (Slack + WhatsApp)
together; WhatsApp via an unofficial local bridge; full operator control from
day one (add goals + approve/deny/ack/rollback over chat).

## 1. What this is - and what it is NOT

This is a **control plane**: the operator uses chat to run ORAC, add goals, ask
status, approve parked actions, roll back recorded commits, and receive review
queue pushes. It is a transport peer to the CLI and the web UI.

It is **NOT** Group 2 Communications, where the agent sends messages as work
through tools such as `channel.send`. The agent gains no messaging powers here.

| | Chat control plane | Group 2 Communications |
| --- | --- | --- |
| Who acts | the operator | the agent |
| New agent capability | none | `channel.send`, etc. |
| Governance | injected operator commands still go through ORAC's broker/council | each send is a broker tool under the risk model |

The gateway is a thin transport, not a second autonomous agent. We borrow
channel-bridge tech, not another agent runtime. State changes flow through the
same ORAC surfaces as the CLI.

## 2. Architecture

```text
WhatsApp (Node Baileys sidecar) --\
                                   -> Gateway -> board/broker/reviews/git.revert
Slack (Bolt Socket Mode) ---------/
```

- **Channels** normalize each platform to `InboundMessage(sender, text, channel)`
  and `OutboundMessage(target, text)`.
- **Slack** uses `slack_bolt` in Socket Mode: official, no public webhook/URL,
  works behind home NAT.
- **WhatsApp** uses a thin Node sidecar that exposes a localhost HTTP bridge.
  ORAC stays Python; only the WhatsApp transport is Node.
- **Gateway** performs auth, rate limiting, parsing, natural-language read-side
  replies, outbound review pushes, and broker-backed control actions.

## 3. Auth

ORAC runs shell/git on your machine. Chat control is therefore a remote command
front door. v1 hardening:

1. Per-channel sender allowlist. Unknown senders are ignored and logged.
2. No privileged path. Commands still pass the broker/council/risk model.
3. Exact IDs are required for `approve`, `deny`, `ack`, and `rollback`.
4. Inbound rate limit per sender.
5. Secrets are stored in the DPAPI-backed credential store; config/logs only
   carry credential refs and redacted status.

## 4. Inbound Control

State-changing actions stay on a small, predictable grammar:

| Message | Action |
| --- | --- |
| `goal: <text>` or `/goal <text>` | create a `code` goal task |
| `status` | active/blocked/done/backlog counts + review queue summary |
| `reviews` | pending approvals + unacked notifications |
| `approve <id>` / `deny <id>` | resolve a parked approval |
| `ack <id>` | acknowledge a completed action |
| `rollback <id>` | `git.revert` the recorded commit as the human principal |
| `help` | the command list |

Free-form operator questions are allowed for read-side intent: status, reviews,
blocked-work triage, and ordinary goal creation phrases. The gateway asks the
configured local LM Studio reasoning model to phrase the answer, using only live
ORAC facts. If the model is unavailable, it falls back to the factual summary
instead of dumping `help`. Destructive actions still require the exact grammar
above.

The chat reply model preference is:

1. `lmstudio_code_model` for reasoning/understanding work, e.g.
   `qwen3.6-35b-a3b`.
2. `lmstudio_small_model`, e.g. `google/gemma-4-12b`.
3. `lmstudio_standard_model`.

## 5. Outbound Push

The gateway pushes the review queue to the authorized sender on state change:
new pending approvals, new review notifications, and queue-clear transitions.
It does not spam every tick.

## 6. Local Sign-On and Runtime

Open `http://localhost:8765`, then use **Settings -> Connections**:

- Slack: paste the `xoxb-...` bot token and `xapp-...` Socket Mode app token,
  then allow your Slack user id (`U...`).
- WhatsApp: click **Start Bridge**, scan the QR when shown, click **Pair
  WhatsApp**, then allow your phone number in E.164 form (`+614...`).
- Click **Start Control** to run the live Slack/WhatsApp connector loop.
- Use the restart buttons in the same pane after code or bridge changes.

Process logs and message JSONL are local under `.orac/comms_logs/`.

## 7. CLI Runbook

Start the ORAC cockpit:

```powershell
$env:PYTHONPATH="D:\Code\ORAC\src"
python -m orac.cli --root D:\Code\ORAC ui
```

Install the optional Slack Python dependency first if needed:

```powershell
pip install -e ".[chat]"
```

The bridge and connector runner can still be launched manually for debugging:

```powershell
$env:PYTHONPATH="D:\Code\ORAC\src"
python -m orac.cli --root D:\Code\ORAC chat whatsapp-bridge
python -m orac.cli --root D:\Code\ORAC chat run
```

## 8. Risks

- WhatsApp bridge is unofficial and may trigger WhatsApp account risk. Prefer a
  secondary number.
- Slack is official Socket Mode and should be the more durable connector.
- The bridge adds a Node runtime beside Python ORAC.
- The control plane does not grant the agent new comms powers.
