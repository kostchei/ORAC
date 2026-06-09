# ORAC

ORAC is a local "scrum of agents" runner. It keeps a small task board on disk, plans work into a sprint, and lets the core ORAC agents move tasks through a delivery workflow.

The first version is dependency-light and runs fully offline. If Ollama is running locally, ORAC can ask a local model to generate richer agent notes; otherwise it uses deterministic built-in agent behavior.

## Quick start

Double-click `ORAC` on the Windows desktop. The launcher initializes the board, starts the local UI, and opens `http://127.0.0.1:8765`.

The commands below are only for development or troubleshooting.

```powershell
cd D:\code\ORAC
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
orac init
orac add "Create project architecture" --desc "Define the package layout and initial CLI." --points 3
orac sprint plan --capacity 5
orac scrum run --cycles 4
orac list
orac intent inspect <task-id>
orac intent protocol
orac agents list
orac agents protocol optimiser
orac tools list
orac ui
```

The task board is stored at `.orac/board.json`.

## Intent agreement gate

Intent uses a coded backbone inspired by Nate B. Jones's clarity-of-intent pattern. Rough tasks do not proceed straight to build. Intent runs a silent scan, asks one clarification question at a time, produces an echo check, and only releases the task after intent is locked.

```powershell
orac intent inspect <task-id>
orac intent answer <task-id> --field purpose --value "..."
orac intent answer <task-id> --field audience --value "..."
orac intent answer <task-id> --field must_include --value "..."
orac intent answer <task-id> --field success_criteria --value "..."
orac intent answer <task-id> --field format --value "..."
orac intent answer <task-id> --field tech_stack --value "..."
orac intent answer <task-id> --field edge_cases --value "..."
orac intent answer <task-id> --field risk_tolerance --value "..."
orac intent lock <task-id>
```

Use `orac intent blueprint <task-id>` for a short plan, `orac intent risk <task-id>` for failure modes, and `orac intent reset <task-id>` to restart intent from scratch.

## Registry and UI

Base requests can be added from the CLI or the local UI:

```powershell
python -m orac.cli registry base-request "Build a feature" --desc "Rough user request"
python -m orac.cli ui --port 8765
```

Open `http://127.0.0.1:8765`. The UI shows the task registry, local resources, current model routing decision, and a color-coded interaction log:

- User entries are blue.
- Agent entries are purple.
- Registry/system logs are green.

If the machine exposes a microphone or speaker, the UI shows audio availability. After clicking `Enable Audio`, the browser asks for microphone permission. Recorded speech is posted to the ORAC backend for transcription with OpenAI Whisper when the optional audio dependencies are installed. The transcript is added to the base request detail field. `Speak` uses local text-to-speech through `pyttsx3` when available, with Windows SAPI as a fallback.

The desktop setup on this machine has the audio stack installed. If rebuilding elsewhere, install it with:

```powershell
pip install -e .[audio]
```

Whisper requires `ffmpeg` on `PATH` for common browser audio formats such as WebM.

Runtime parameters are editable in the UI under `Settings`, including monthly budget, estimated foundation cycle cost, agent wake interval, cycles per tick, LM Studio URL, and standard/small local model IDs. The daily budget, 60% foundational fraction, and 60% local resource target are fixed in code by design.

## 24/7 Loop and Model Routing

ORAC can run continuously:

```powershell
python -m orac.cli daemon run --interval 60 --cycles 1
```

The model policy defaults to:

- 60% local resource target before backing off.
- $20/month online foundational budget.
- $0.75/day planning budget.
- 60% of the daily budget available to foundational access, so the default daily foundational cap is $0.45.
- $0.05 estimated foundational spend recorded per productive agent cycle unless you change the policy config.

When the daily foundational cap is exhausted, ORAC routes back to local models. When local CPU, memory, GPU, or VRAM use is high, ORAC chooses the smaller local model tier.

## LM Studio

ORAC expects LM Studio's local OpenAI-compatible server at `http://localhost:1234/v1` by default. When ORAC starts, it starts the LM Studio server if the `lms` CLI is available. If a local model is already loaded, ORAC keeps it. If no model is loaded, ORAC checks available RAM and loads the largest suitable local model it can fit within the resource policy, preferring tool-use models when possible.

Useful commands:

```powershell
python -m orac.cli models lmstudio-start --port 1234
python -m orac.cli models lmstudio-status
python -m orac.cli models lmstudio-models
python -m orac.cli models policy
```

Set these environment variables to name your local models:

```powershell
$env:ORAC_LMSTUDIO_MODEL = "your-standard-model-id"
```

For online foundational access, set:

```powershell
$env:ORAC_FOUNDATION_API_KEY = "..."
$env:ORAC_FOUNDATION_MODEL = "..."
```

## Optional local LLM

Start Ollama and set a model:

```powershell
$env:ORAC_MODEL = "llama3.2"
orac scrum run --cycles 2 --brain ollama
```

If Ollama is unavailable, use `--brain rules` for the built-in offline brain.

## Concepts

- Orchestrator coordinates the loop and reports back to the main task.
- Intent removes ambiguity and ensures the actual goal is met.
- Optimiser manages resources and aims to spend up to 60% of available resources by default.
- Efficiency looks for waste, dead code, and unnecessary structure.
- Simples finds the most effective path with the least number of components.

Agent prompts and JSON-style protocols live in `src/orac/prompts/`. Intent's executable backbone lives in `src/orac/intent_backbone.py`. Karpathy-inspired agent operating notes live in `docs/karpathy_agent_guidelines.md`. Regular-use tool definitions live in `src/orac/tools/catalog.json`, and their local implementations live in `src/orac/tooling.py`. The task registry lives in `src/orac/task_registry.py`, resource checks live in `src/orac/resources.py`, and model routing lives in `src/orac/model_policy.py`. The manifest at `src/orac/prompts/agents.json` binds each agent to its prompt, protocol, and allowed tools.

This repo is intentionally small so the orchestration surface is easy to change as the agent system grows.
