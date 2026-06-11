# Model Selection & Routing

How ORAC decides which mind runs which work. Two axes: **which local model** (per work
kind) and **local vs foundation** (per call site and stakes). Hardware envelope: one
16 GB GPU, LM Studio serving an OpenAI-compatible API.

## The local lineup (16 GB VRAM, mid-2026)

| Slot | Model | Footprint | Why |
| --- | --- | --- | --- |
| Resident default (sessions, lenses) | **GPT-OSS-20B** (MXFP4) | ~13.7 GB @ 60K ctx, ~42 t/s | Best logic/code at this tier with real KV headroom; fast at long context |
| Heavy `code` sessions | **Qwen3-Coder-Next** | MoE, ~3B active — OK with expert offload | Consensus best local coder (Latent.Space 4/2026) |
| Creative / `media` / `event` | **Mistral Small 3.1 24B** | ~13–14 GB @ Q4, ~55 t/s | Best creative all-rounder at this tier; vision |
| Quality-ceiling experiment | Qwen3.6-27B (low quant) | 16.8 GB @ Q4_K_M — over budget | Worth one A/B vs GPT-OSS on a real Builder session; not the default |

Config keys (`.orac/config.json → model_policy`): `lmstudio_standard_model` (resident),
`lmstudio_code_model`, `lmstudio_creative_model`, `lmstudio_small_model` (busy-box
fallback). Set them to the LM Studio model keys once the models are pulled.

> Pulling/downloading models should itself become an agent task (a `model.download`
> capability for the Operator-of-models). Until then: pull manually in LM Studio.

## Structured output: format is enforced, not requested

LM Studio enforces JSON schemas at the token level (`response_format.json_schema`,
outlines-style constrained decoding). ORAC therefore sends the session **decision
schema** and the driver **origination schema** with every structured call: the server
cannot emit invalid JSON for any model ≥~7B. Model choice affects decision *quality*
only; format reliability is the runtime's job. (`Brain.think_json`; brains without
structured support fall back to plain `think` + strict parsing, which blocks the
session on prose — visible, never silent.)

## Local vs foundation routing

Principle: **spend foundation tokens where one decision steers hours of local work;
spend local tokens where volume lives.**

| Call site | Volume | Leverage | Brain |
| --- | --- | --- | --- |
| Driver origination (≤3/day) | tiny | steers everything downstream | foundation |
| Orchestrator decomposition → contracts | low | high | foundation |
| Doer sessions (Builder, …) | high | medium | local, per work-kind slot |
| LLM lenses (P5 council reviews) | high | medium | local; foundation only for irreversible-external verdicts |
| Deterministic floor / broker / risk table | all calls | — | no model |
| **Retry after a local session blocks** | rare | high | **escalate to foundation** |

The last row is the dynamic rule: don't predict which tasks exceed local capability —
let the local model fail once (blocked sessions are contained and visible), mark the
task `escalated`, and rerun with the foundation model. A second failure stays BLOCKED
for the human. Escalation only happens when a foundation key and budget headroom exist;
otherwise the task stays blocked rather than burning a second local attempt.

Foundation spend remains governed by the existing daily cap
(`daily_foundation_budget_usd × foundation_daily_fraction`) in `model_policy.py`.

## Browser foundation (no API key)

When you don't have or want an API key, set `ORAC_BROWSER_FOUNDATION=claude`
(or `gemini` or `openai`) before starting the daemon.  ORAC connects to an
already-running Chrome/Edge instance via the Chrome DevTools Protocol, opens a
new tab to the provider's chat UI, types the prompt, waits for the response to
stabilise, and reads the text back.

**One-time setup**

```
# 1. Start Chrome with the debugging port open
chrome --remote-debugging-port=9222 --no-first-run

# 2. Log into claude.ai / gemini.google.com / chatgpt.com in that browser

# 3. Tell ORAC to use it
$env:ORAC_BROWSER_FOUNDATION = "claude"   # PowerShell
export ORAC_BROWSER_FOUNDATION=claude     # bash/zsh
```

ORAC uses its own small `orac.browser_primitive` CDP/WebSocket client for the
browser tab operations, so browser foundation no longer requires Playwright or a
third-party websocket package.

Or set `browser_foundation_provider` in `.orac/config.json → model_policy`.

**Routing behaviour**

Browser foundation is used exactly where the API-key path would be used:
origination, decomposition, and escalated sessions.  When both an API key *and*
browser foundation are configured, the API key wins (controlled spend).  All the
same daily-cap and escalation logic applies; browser calls aren't tracked by cost
(they're free) so `can_escalate` returns `True` unconditionally in browser mode.

**Reliability notes**

- Chat UIs enforce rate limits.  Don't set `daemon_interval_seconds` below 60.
- The CSS selectors in `browser_brain.py` will break when providers redesign
  their UIs.  Update `_INPUT_SELECTORS` / `_RESPONSE_SELECTORS` if a call
  returns empty or wrong text.
- For `think_json` calls, the schema is included as plain-text instructions in
  the prompt.  Server-side token enforcement is unavailable.  `parse_decision`
  in AgentSession still rejects malformed replies, so failures are visible rather
  than silent.
- The brain blocks the daemon thread while the browser tab is open.  120 s
  default timeout is generous; reduce `timeout_seconds` if you want faster
  failure on a hung provider.

## Model swapping discipline

16 GB holds one of the above at a time, so kind-switches mean an LM Studio load
(10–60 s). Rules: batch same-kind tasks before switching; never swap mid-session; the
busy-box rule (external load detected → `lmstudio_small_model`) takes precedence over
kind preference. Swap thrash is an Optimise-lens concern: if swaps/hour climbs, that is
the fair-share governor's signal to batch harder.
