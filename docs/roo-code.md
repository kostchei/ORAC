# Roo Code + LM Studio

ORAC is set up to use Roo Code with LM Studio's local OpenAI-compatible server.
The current local coding model is **Qwen3 Coder Next** (`qwen/qwen3-coder-next`),
the 80B-total / 3B-active model already present in LM Studio. LM Studio is
serving it on port `1234` with automatic GPU offload for the 24 GB RTX 4090.

## Roo Code settings

Install or enable the recommended `RooVeterinaryInc.roo-cline` extension, then
open Roo Code's provider settings and choose:

| Setting | Value |
| --- | --- |
| API Provider | LM Studio |
| Base URL | `http://localhost:1234` |
| Model ID | `qwen/qwen3-coder-next` |
| API token | Paste the token saved from ORAC Settings → Connections → LM Studio |

Roo uses the server's OpenAI-compatible `/v1` routes behind that base URL. The
project rules in `.roo/rules/ORAC.md` are loaded automatically by Roo Code.

## Start and verify

```powershell
lms server start --port 1234
lms ps --json
python -m orac.cli models lmstudio-status
python -m orac.cli models lmstudio-models
```

If the model is not loaded, load the largest local coder with enough context
for repository work:

```powershell
lms load qwen/qwen3-coder-next --context-length 65536 --yes
```

LM Studio authentication is enabled on this machine. Paste the API token in the
ORAC popup rather than putting it in `.orac/config.json`, source files, or Git.
ORAC stores it in the Windows DPAPI-backed credential vault at runtime.
