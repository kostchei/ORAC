from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from orac.resources import ResourceSnapshot, read_resource_snapshot
from orac.storage import BoardStore


DEFAULT_POLICY = {
    "monthly_foundation_budget_usd": 20.0,
    "daily_foundation_budget_usd": 0.75,
    "foundation_daily_fraction": 0.60,
    "estimated_foundation_cycle_usd": 0.05,
    "target_local_resource_percent": 60.0,
    "lmstudio_url": "http://localhost:1234/v1",
    "lmstudio_standard_model": "",
    "lmstudio_small_model": "",
    # Per-work-kind model slots (docs/model-selection.md). Empty = use standard.
    "lmstudio_code_model": "",
    "lmstudio_creative_model": "",
    "lmstudio_identifier": "orac-local",
    "lmstudio_autoload_on_start": True,
    "daemon_interval_seconds": 60,
    "daemon_cycles": 1,
    # Browser foundation (docs/model-selection.md §browser).
    # Empty string = disabled. Set to "claude", "gemini", or "openai" to route
    # foundation calls through the provider's chat UI instead of an API key.
    # Chrome must be running with --remote-debugging-port=9222.
    "browser_foundation_provider": "",
    "browser_cdp_url": "http://localhost:9222",
}

# Local-vs-foundation routing by call site (docs/model-selection.md): foundation
# where one decision steers hours of local work, local where volume lives.
ROUTING = {
    "origination": "foundation",
    "decomposition": "foundation",
    "session": "local",
    "lens": "local",
    "escalation": "foundation",
}

# Which local model slot serves which work kind.
_KIND_MODEL_SLOTS = {
    "code": "lmstudio_code_model",
    "media": "lmstudio_creative_model",
    "event": "lmstudio_creative_model",
}


@dataclass(frozen=True)
class ModelDecision:
    brain: str
    model: str
    reason: str
    daily_foundation_cap_usd: float
    foundation_spent_today_usd: float
    foundation_remaining_today_usd: float
    resources: ResourceSnapshot

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["resources"] = self.resources.to_dict()
        return data


class ModelPolicyStore:
    def __init__(self, store: BoardStore) -> None:
        self.store = store

    def load_policy(self) -> dict[str, Any]:
        policy = self.store.load_json(self.store.config_path, {"model_policy": DEFAULT_POLICY})
        merged = dict(DEFAULT_POLICY)
        merged.update(policy.get("model_policy", {}))
        return merged

    def save_policy(self, policy: dict[str, Any]) -> None:
        config = self.store.load_json(self.store.config_path, {})
        merged = dict(DEFAULT_POLICY)
        merged.update(policy)
        merged = _coerce_policy(merged)
        config["model_policy"] = merged
        self.store.save_json(self.store.config_path, config)

    def usage(self) -> dict[str, Any]:
        return self.store.load_json(self.store.usage_path, {"foundation": {}})

    def record_foundation_spend(self, amount_usd: float) -> None:
        usage = self.usage()
        key = _today_key()
        foundation = usage.setdefault("foundation", {})
        foundation[key] = round(float(foundation.get(key, 0.0)) + amount_usd, 6)
        self.store.save_json(self.store.usage_path, usage)

    def decide(self) -> ModelDecision:
        policy = self.load_policy()
        resources = read_resource_snapshot(float(policy["target_local_resource_percent"]))
        daily_cap = round(
            float(policy["daily_foundation_budget_usd"])
            * float(policy["foundation_daily_fraction"]),
            4,
        )
        spent = self.spent_today()
        remaining = round(max(0.0, daily_cap - spent), 4)

        if resources.busy:
            model = str(policy.get("lmstudio_small_model") or "small_local")
            return ModelDecision(
                brain="lmstudio",
                model=model,
                reason=f"heavy local use detected; using smaller local model ({resources.reason})",
                daily_foundation_cap_usd=daily_cap,
                foundation_spent_today_usd=spent,
                foundation_remaining_today_usd=remaining,
                resources=resources,
            )
        if remaining > 0 and _has_foundation_key():
            return ModelDecision(
                brain="foundation",
                model="foundation",
                reason="foundation budget remains for today",
                daily_foundation_cap_usd=daily_cap,
                foundation_spent_today_usd=spent,
                foundation_remaining_today_usd=remaining,
                resources=resources,
            )
        provider = _browser_provider(policy)
        if provider:
            return ModelDecision(
                brain="browser",
                model=provider,
                reason=f"browser foundation active (provider={provider}; no API key required)",
                daily_foundation_cap_usd=daily_cap,
                foundation_spent_today_usd=spent,
                foundation_remaining_today_usd=remaining,
                resources=resources,
            )
        model = str(policy.get("lmstudio_standard_model") or "local")
        return ModelDecision(
            brain="lmstudio",
            model=model,
            reason="foundation budget unavailable or exhausted; using local LM Studio",
            daily_foundation_cap_usd=daily_cap,
            foundation_spent_today_usd=spent,
            foundation_remaining_today_usd=remaining,
            resources=resources,
        )

    def spent_today(self) -> float:
        usage = self.usage()
        return round(float(usage.get("foundation", {}).get(_today_key(), 0.0)), 4)

    def estimated_cycle_spend(self) -> float:
        return round(float(self.load_policy()["estimated_foundation_cycle_usd"]), 4)


def model_for_work_kind(policy: dict[str, Any], work_kind: str | None) -> str:
    """The local model slot for a work kind; standard model when no slot is set."""
    slot = _KIND_MODEL_SLOTS.get(work_kind or "")
    if slot and policy.get(slot):
        return str(policy[slot])
    return str(policy.get("lmstudio_standard_model") or "local")


def can_escalate(policy_store: "ModelPolicyStore") -> bool:
    """Escalation to foundation requires either:
    - an API key with remaining daily budget, or
    - browser foundation configured (no cost tracking).
    """
    if _has_browser_foundation(policy_store.load_policy()):
        return True
    if not _has_foundation_key():
        return False
    policy = policy_store.load_policy()
    daily_cap = round(
        float(policy["daily_foundation_budget_usd"])
        * float(policy["foundation_daily_fraction"]),
        4,
    )
    return policy_store.spent_today() < daily_cap


def session_brain_for(policy_store: "ModelPolicyStore", task: Any) -> Any:
    """The brain for a doer session (docs/model-selection.md routing).

    Escalated tasks (a local session already failed on them) get the foundation
    brain (API or browser); everything else runs locally on the work kind's model
    slot. Callers must check :func:`can_escalate` before marking a task escalated.
    """
    from orac.llm import build_brain

    if task.metadata.get("escalated"):
        policy = policy_store.load_policy()
        provider = _browser_provider(policy)
        if provider:
            return build_brain("browser", model=provider)
        return build_brain("foundation")
    policy = policy_store.load_policy()
    return build_brain("lmstudio", model=model_for_work_kind(policy, task.work_kind))


def _today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _has_foundation_key() -> bool:
    return bool(os.environ.get("ORAC_FOUNDATION_API_KEY"))


def _browser_provider(policy: dict[str, Any]) -> str:
    """Return the browser foundation provider name, or '' if disabled.

    Resolution order: ORAC_BROWSER_FOUNDATION env var → policy key.
    """
    env = os.environ.get("ORAC_BROWSER_FOUNDATION", "")
    if env:
        return env
    return str(policy.get("browser_foundation_provider", ""))


def _has_browser_foundation(policy: dict[str, Any]) -> bool:
    return bool(_browser_provider(policy))


def _coerce_policy(policy: dict[str, Any]) -> dict[str, Any]:
    numeric_float = [
        "monthly_foundation_budget_usd",
        "daily_foundation_budget_usd",
        "foundation_daily_fraction",
        "estimated_foundation_cycle_usd",
        "target_local_resource_percent",
    ]
    for key in numeric_float:
        policy[key] = float(policy[key])
    policy["daemon_interval_seconds"] = max(5, int(policy["daemon_interval_seconds"]))
    policy["daemon_cycles"] = max(1, int(policy["daemon_cycles"]))
    for key in [
        "lmstudio_url",
        "lmstudio_standard_model",
        "lmstudio_small_model",
        "lmstudio_code_model",
        "lmstudio_creative_model",
        "lmstudio_identifier",
        "browser_foundation_provider",
        "browser_cdp_url",
    ]:
        policy[key] = str(policy.get(key, ""))
    policy["lmstudio_autoload_on_start"] = bool(policy.get("lmstudio_autoload_on_start", True))
    return policy


def lmstudio_status() -> dict[str, Any]:
    if shutil.which("lms") is not None:
        try:
            completed = subprocess.run(
                ["lms", "server", "status", "--json", "--quiet"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    return _lmstudio_http_status()


def lmstudio_loaded_models() -> list[dict[str, Any]]:
    if shutil.which("lms") is None:
        return []
    try:
        completed = subprocess.run(
            ["lms", "ps", "--json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return _parse_json_list(completed.stdout)


def lmstudio_available_model_records() -> list[dict[str, Any]]:
    if shutil.which("lms") is None:
        return []
    try:
        completed = subprocess.run(
            ["lms", "ls", "--llm", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return _parse_json_list(completed.stdout)


def lmstudio_start(port: int = 1234) -> tuple[bool, str]:
    if shutil.which("lms") is None:
        return False, "LM Studio CLI `lms` was not found on PATH."
    try:
        completed = subprocess.run(
            ["lms", "server", "start", "--port", str(port)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output


def lmstudio_load_model(model_key: str, identifier: str = "orac-local") -> tuple[bool, str]:
    if shutil.which("lms") is None:
        return False, "LM Studio CLI `lms` was not found on PATH."
    try:
        completed = subprocess.run(
            ["lms", "load", model_key, "--identifier", identifier, "--yes"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output


def select_lmstudio_model_for_ram(policy: dict[str, Any]) -> dict[str, Any] | None:
    resources = read_resource_snapshot(float(policy["target_local_resource_percent"]))
    records = lmstudio_available_model_records()
    if not records:
        return None

    if resources.busy and policy.get("lmstudio_small_model"):
        return _record_for_model(records, str(policy["lmstudio_small_model"]))
    if not resources.busy and policy.get("lmstudio_standard_model"):
        return _record_for_model(records, str(policy["lmstudio_standard_model"]))

    available_gb = resources.memory_available_gb or 0
    budget_bytes = available_gb * (1024**3) * 0.60
    if resources.busy:
        budget_bytes = available_gb * (1024**3) * 0.35

    candidates = [
        record
        for record in records
        if int(record.get("sizeBytes", 0)) > 0 and int(record.get("sizeBytes", 0)) <= budget_bytes
    ]
    if not candidates:
        candidates = sorted(records, key=lambda record: int(record.get("sizeBytes", 0)))[:1]
    candidates.sort(
        key=lambda record: (
            bool(record.get("trainedForToolUse")),
            int(record.get("sizeBytes", 0)),
        ),
        reverse=True,
    )
    return candidates[0] if candidates else None


def ensure_lmstudio_model_loaded(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = _coerce_policy({**DEFAULT_POLICY, **(policy or {})})
    if os.environ.get("ORAC_SKIP_MODEL_AUTOLOAD") == "1":
        return {"ok": True, "action": "skipped", "message": "Skipped by ORAC_SKIP_MODEL_AUTOLOAD."}
    if not policy["lmstudio_autoload_on_start"]:
        return {"ok": True, "action": "disabled", "message": "LM Studio autoload is disabled."}

    port = _port_from_lmstudio_url(str(policy["lmstudio_url"]))
    server_ok, server_output = lmstudio_start(port)
    loaded = lmstudio_loaded_models()
    if loaded:
        return {
            "ok": True,
            "action": "kept_loaded",
            "message": "LM Studio already has a loaded model.",
            "loaded_models": loaded,
        }

    selected = select_lmstudio_model_for_ram(policy)
    if not selected:
        return {
            "ok": False,
            "action": "no_model",
            "message": "No local LM Studio model was available to load.",
            "server_output": server_output,
        }
    model_key = str(selected.get("selectedVariant") or selected.get("modelKey") or selected.get("path"))
    load_ok, load_output = lmstudio_load_model(model_key, str(policy["lmstudio_identifier"]))
    return {
        "ok": bool(server_ok and load_ok),
        "action": "loaded" if load_ok else "load_failed",
        "selected_model": selected,
        "model_key": model_key,
        "identifier": policy["lmstudio_identifier"],
        "server_output": server_output,
        "load_output": load_output,
    }


def lmstudio_models(base_url: str = "http://localhost:1234/v1") -> list[str]:
    request = Request(f"{base_url.rstrip('/')}/models", method="GET")
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return []
    return [str(item["id"]) for item in payload.get("data", []) if "id" in item]


def _lmstudio_http_status() -> dict[str, Any]:
    models = lmstudio_models()
    return {"running": bool(models), "port": 1234, "models": models}


def _parse_json_list(text: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    start = stripped.find("[")
    if start < 0:
        return []
    try:
        payload = json.loads(stripped[start:])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _record_for_model(records: list[dict[str, Any]], model: str) -> dict[str, Any] | None:
    for record in records:
        if model in {
            str(record.get("modelKey", "")),
            str(record.get("indexedModelIdentifier", "")),
            str(record.get("path", "")),
            str(record.get("displayName", "")),
        }:
            return record
    return None


def _port_from_lmstudio_url(url: str) -> int:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return int(parsed.port or 1234)
    except Exception:
        return 1234
