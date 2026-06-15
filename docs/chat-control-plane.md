# Chat Control Plane — task control from WhatsApp & Slack

**Status:** Scoped (not yet built). **Decisions locked:** both channels (Slack +
WhatsApp) together · WhatsApp via an OpenClaw-style **unofficial bridge** · **full
control** from day one (add goals + approve/deny/ack/rollback over chat).

## 1. What this is — and what it is NOT

This is a **control plane**: *you* operating ORAC over chat — add a goal, ask
status, approve a parked action, roll one back — plus ORAC pushing its review
queue to you. It is a transport, peer to the CLI and the web UI.

It is **NOT** Group 2 Communications (the *agent* sending messages as work, e.g.
`channel.send`). The agent gains no messaging powers here. Keeping these separate
is the core design rule, because it keeps the risk model honest:

| | Chat control plane (this doc) | Group 2 Communications |
| --- | --- | --- |
| Who acts | the operator (you) | the agent (ORAC) |
| New agent capability | **none** | `channel.send`, etc. |
| Governance | every injected command goes through the broker/council, like the CLI | each send is a broker tool under the risk model |

**The non-negotiable architectural constraint:** the gateway is a thin transport,
**not** a second autonomous agent. We borrow OpenClaw's *channel-bridge tech*, not
its agent runtime — running OpenClaw's own LLM/agent loop beside ORAC would create
a second brain with a privileged path that bypasses the council, which defeats
ORAC's entire thesis. Inbound chat messages map to existing operator commands;
state changes flow through the broker exactly as they do from the CLI.

## 2. Architecture (Gateway + Channels, wired to ORAC's existing surface)

```text
WhatsApp ──┐                              ┌── add goal ─────────► board/broker
  (Node    │                              │── approve/deny/ack ─► pending machinery
  Baileys  ├─► Channel adapters ─► Gateway ┤── rollback ─────────► git.revert (human principal)
  sidecar) │   (normalise in/out)  (auth + │── status/reviews ──► review_queue_summary
Slack ─────┘                       parse + │
  (Bolt,                           route)  └── push ◄──────────── notify queue (P6)
  Socket Mode)
```

- **Channels** normalise each platform to a common `InboundMessage(sender, text,
  channel)` / `OutboundMessage(target, text)`.
  - **Slack** — `slack_bolt` in **Socket Mode**: official, ToS-clean, **no public
    webhook/URL** (works behind home NAT), DMs + slash commands.
  - **WhatsApp** — a thin **Node bridge sidecar** (Baileys / whatsapp-web.js — the
    same unofficial WhatsApp-Web tech OpenClaw uses) exposing a localhost socket
    that ORAC's Python gateway talks to. ORAC stays Python; only the WhatsApp
    transport is Node.
- **Gateway** (native ORAC, Python) — auth check → parse → route to the existing
  command surface; and the outbound pump that consumes `review_queue_summary`.
  Reuses, not reinvents: [notify.py](../src/orac/notify.py) for push,
  `cmd_add` / `cmd_reviews` / `cmd_approve` / `cmd_deny` / `cmd_ack` /
  `cmd_rollback` for the operations.

## 3. Auth — the whole ballgame (full control = a remote-command front door)

ORAC runs shell/git on your machine. A chat that can add goals and approve actions
is a remote into that. v1 hardening, all mandatory:

1. **Per-channel sender allowlist.** Exactly the authorized sender id(s) — Slack
   user id(s), WhatsApp phone number(s). Every other sender is **ignored and
   logged**, never parsed.
2. **No privileged path.** Injected commands still pass the broker/council/risk
   model. Adding a goal just creates a task; approving resolves the *exact* parked
   request — identical to the CLI.
3. **Echo-and-confirm for `approve`-class actions.** Approving a comms/financial/
   physical action over chat echoes the action + a one-time confirm token before
   it resolves, so a single stray message can't authorize an irreversible act.
4. **Inbound rate limit** (per sender) so a flooded channel can't drive ORAC.
5. **Secrets via a mini credential store** (DPAPI / Windows Credential Manager;
   opaque `credential_ref`; redaction at the logging layer). This is the first,
   small cut of the Group 2 **credential vault** — 1–2 bot tokens + the WhatsApp
   session, not the full surface.

## 4. Inbound command grammar (v1: deterministic, not free-text)

A small, predictable parser (an LLM free-text mode is a later option):

| Message | Action |
| --- | --- |
| `goal: <text>` or `/goal <text>` | create a `code` goal task (READY) |
| `status` | `review_queue_summary` + active/blocked task counts |
| `reviews` | list pending approvals + unacked actions (ids + one-line each) |
| `approve <id>` / `deny <id>` | resolve a parked approval (with echo-confirm) |
| `ack <id>` | accept a completed action |
| `rollback <id>` | `git.revert` the recorded commit (human principal) |
| `help` | the command list |

Unknown text → a `help` nudge, never a silent drop.

## 5. Outbound push (the cheap, high-value half)

The daemon already computes `review_queue_summary` each tick. The gateway pushes
to the operator's channel on **state change** (not every tick — no spam):
a new pending approval ("needs your approval: …"), a task done/blocked, queue
went non-empty. This is the "real push channel" [notify.py](../src/orac/notify.py)
already anticipates.

## 6. Build order (push first, then control — both ship in v1)

0. **Secrets + config + allowlist** — mini credential store; `chat` block in
   `config.json` (channels, authorized senders, enable flags). Prereq.
1. **Outbound push** — notify queue → Slack + WhatsApp. Read-only + alerts.
2. **Inbound control** — the grammar → broker/CLI ops, with auth + echo-confirm +
   rate limit.
3. **Connectors** — Slack Bolt (Socket Mode) live; the Node WhatsApp bridge
   sidecar live (one-time QR login of the number).

## 7. Dependencies & risks

- **Setup the operator must do once:** create a Slack app (bot + app tokens, Socket
  Mode); QR-login the WhatsApp number into the Node bridge.
- **WhatsApp ban risk (accepted).** The unofficial bridge is against WhatsApp ToS
  and can get a number flagged/banned; use a secondary number, not your primary.
  This **deviates from [tool-categories.md](tool-categories.md)** ("prefer official
  APIs; no browser automation for messaging") — a deliberate, recorded choice for
  the control plane (convenience over durability); Group 2 *sending* should still
  prefer the official Cloud API.
- **Two runtimes** — a Node sidecar for WhatsApp alongside Python ORAC. Slack is
  pure Python.
- **Reuses, no new agent power:** broker, council, notify queue, park/resume, the
  command surface — all already exist.

## 8. Done state

You text ORAC "status" from your phone and get the review queue; it texts you when
a task needs approval; you reply "approve 3" (with confirm) and the parked action
resolves on the next tick — all from an allowlisted sender, every action through
the same council the CLI uses, no second agent in the loop.
