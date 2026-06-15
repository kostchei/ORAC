# Tool Categories (the capability surface)

The five categories of work ORAC will eventually do. Each is an adapter family plugged into the
same broker + council + risk model — none get a privileged path. Ordering and dependencies are
in [roadmap.md](roadmap.md); governance is in [edge-check-council-design.md](edge-check-council-design.md).

Each category is annotated with its dominant **risk class** (reversibility × externality) and
what it **depends on** before it can ship.

---

## 1. Code Writing  ·  *reversible-via-checkpoint · local→external(git)*  ·  **the bootstrap**

A code adapter layer. This is Milestone A: the Builder's first real powers, and what unlocks
self-improvement. Reversibility is *engineered* — work on a branch / commit before changing
files — so most of it is `auto + notify`; `push` / PR is the external step that gates.

**Candidate substrates (decide before the write slice):**
- Roo Code / Roomote-style tooling for editor/code-agent workflows.
- Codex / local shell adapter for repo work.
- Git adapter for branches, diffs, tests, commits.
- Browser verification adapter for frontend checks.

**Rules (= the risk model, instantiated):**
- Builder works in branches or isolated worktrees.
- Writes allowed **only inside approved repo roots**.
- Shell commands split into risk classes (read vs. mutate vs. destructive).
- PR/commit-push requires explicit approval unless a standing grant exists.
- Tests + diff summary required before a task may reach `done`.

**Tools:** `repo.read_file` · `repo.search` · `repo.apply_patch` · `repo.run_tests` ·
`git.create_branch` · `git.stage` · `git.commit` · `browser.verify_local_app`

**Depends on:** P0 (types), P1 (risk model), Builder role + privilege test (§4.6).

---

## 2. Communications  ·  *irreversible · external-private/public*  ·  approval-first

> **Not the same as the chat control plane.** Operating ORAC *from* WhatsApp/Slack
> (add a goal, approve a review) is a **control plane / transport**, peer to the CLI —
> the agent gains no messaging power. That is scoped separately in
> [chat-control-plane.md](chat-control-plane.md) and, by explicit decision, may use an
> unofficial WhatsApp bridge (convenience over durability). **This section is the
> other thing:** the *agent* sending messages as work. For that, the rules below hold.

**Do not start with browser automation for messaging.** Prefer official APIs. Model as two
distinct capabilities — `channel.read` and `channel.send` — and default sends to **draft mode**:
ORAC drafts → user approves → connector sends.

**References:**
- Slack scopes/token types for granular permissions — https://docs.slack.dev/reference/scopes
- WhatsApp Business Cloud API / webhooks — https://developers.facebook.com/docs/whatsapp/cloud-api/overview
- LinkedIn API access (restricted, product-gated) — https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access
- Reddit OAuth scopes per action — https://www.reddit.com/dev/api/oauth

**Tools:** `slack.read_thread` · `slack.draft_reply` · `slack.send_reply` ·
`whatsapp.send_template` · `reddit.read_inbox` · `reddit.draft_comment` · `linkedin.draft_post`

WhatsApp / LinkedIn / Facebook are **high-risk** (account reputation + platform policy). Require
approval initially, no standing grants.

**Depends on:** the **credential vault** (hard blocker — no real send without opaque
`credential_ref` + log redaction), P6 (draft/approve maturity).

---

## 3. Media Generation  ·  *reversible (until publish) · local compute*  ·  job-queue

Use **job queues, not blocking calls**. ComfyUI fits — it already works around workflow JSON and
queue execution (submit to `/prompt`, monitor queue/status/websocket):
https://docs.comfy.org/development/comfyui-server/comms_routes

**Subsystem objects:** `media_job` · `workflow_template` · `asset_store` · `review_state` ·
`publish_permission`

**Tools:** `comfy.workflow_list` · `comfy.generate_image` · `comfy.queue_status` ·
`comfy.fetch_artifact` · `fishspeech.generate_audio` · `media.review_asset` · `media.publish_asset`

- Fish Speech (local/open TTS, CLI/WebUI/server) — **check license + voice-cloning legal
  disclaimer before integrating**: https://github.com/fishaudio/fish-speech
- Text generation: treat as a **provider interface** (LM Studio / Ollama / OpenAI-compatible /
  other), not coupled to any single endpoint. (ORAC already abstracts this in `model_policy.py`.)

Generation is reversible (artifacts sit in `review_state`); **publish** is the gated, external
step.

**Depends on:** P2/P3 (review→publish maps onto council + pending), an artifact/asset store.

---

## 4. Physical Devices  ·  *irreversible · physical*  ·  approval + e-stop

Use an **automation hub, not per-device hacks**.

**First layer:**
- Home Assistant — broad, local-first device control: https://www.home-assistant.io/integrations/
- MQTT — standard IoT pub/sub: https://mqtt.org/
- Node-RED — visual/event-driven flows: https://nodered.org/
- ONVIF — compatible network cameras: https://www.onvif.org/profiles-specifications-new/

**Safety rules (non-negotiable):**
- Every device gets `read_state`, `prepare_action`, `execute_action` (separate capabilities).
- Destructive/physical actions require approval by default.
- Emergency stop. Cooldowns + rate limits (`rate_counters`). Log every action (audit).
- Prefer local APIs over cloud.
- **Never** let agents auto-discover and control arbitrary LAN devices.

**Tools:** `homeassistant.list_entities` · `homeassistant.read_state` ·
`homeassistant.call_service` · `mqtt.publish` · `camera.snapshot` · `camera.ptz_move` ·
`printer.submit_job` · `vacuum.start_cleaning` · `solar.read_status`

**Depends on:** P6 (standing grants + rate caps — the fish-feeder case), e-stop primitive.

---

## 5. Human Events / Games / Interactive Tasks  ·  *session workflow*  ·  separate epic

Model as **sessions**. This is a workflow layer that *consumes* the broker — it should not bundle
a workflow engine into the broker itself.

**Core objects:** `event` · `participant` · `round` · `state` · `timer` · `human_input_required`
· `broadcast_channel`

**Tools:** `event.create` · `event.add_participant` · `event.ask_human` · `event.wait_for_response`
· `event.advance_round` · `event.broadcast_update` · `event.close`

Lets ORAC run a game, workshop, meeting, checklist, or multi-human task without special-casing
each. `human_input_required` reuses the same park/resume machinery as `pending_approval`.

**Depends on:** everything above being stable; explicitly the **last** epic.
