from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from orac.models import Task


# Measured foundation spend (TODO: replace the $0.05/cycle estimate). Only models
# listed here accrue cost; anything else — every local LM Studio model — is free
# and accrues nothing, so local volume never touches the budget. Prices are USD
# per 1M tokens (input, output), approximate published rates mid-2026; keep current
# when adding a foundation model. An unlisted FOUNDATION model accrues 0 (a visible
# under-count to fix by adding it here, never a silent overcharge of local work).
FOUNDATION_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}

_measured_foundation_spend_usd = 0.0


def record_llm_usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Accrue measured foundation spend from one API response's token usage.

    Central seam (every OpenAI-compatible call lands here regardless of which
    rotating brain instance made it), so spend is captured even though foundation
    work now happens across on-demand planning/escalation brains. Browser
    foundation never reaches this path (it's not an API call) — it's free.
    """
    price = FOUNDATION_PRICING_USD_PER_MTOK.get(model)
    if not price or not (prompt_tokens or completion_tokens):
        return
    global _measured_foundation_spend_usd
    cost = prompt_tokens / 1_000_000 * price[0] + completion_tokens / 1_000_000 * price[1]
    _measured_foundation_spend_usd = round(_measured_foundation_spend_usd + cost, 6)


def drain_foundation_spend_usd() -> float:
    """Return measured foundation spend accrued since the last drain, then reset.

    The daemon / UI tick / scrum-run drains this after each run and records it in
    place of the old flat estimate — measured usage, not a placeholder. A tick with
    only local/browser work drains 0 (both are free)."""
    global _measured_foundation_spend_usd
    spent = _measured_foundation_spend_usd
    _measured_foundation_spend_usd = 0.0
    return spent


class Brain(Protocol):
    def think(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        ...


# Brains MAY additionally provide structured output:
#   think_json(agent_name, role, task, prompt, schema) -> str
# where the backend enforces the JSON schema at the token level (LM Studio /
# OpenAI-compatible response_format). Callers detect the capability with
# getattr; brains without it get plain think() plus strict parsing downstream.


@dataclass
class RulesBrain:
    def think(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        del prompt
        if role == "intent":
            return f"Clarified '{task.title}' into a checkable goal with explicit acceptance criteria."
        if role == "optimiser":
            return f"Budgeted '{task.title}' to use no more than 60% of available resources by default."
        if role == "simples":
            return f"Chose the smallest effective path for '{task.title}' and advanced the work."
        if role == "efficiency":
            return f"Reviewed '{task.title}' for waste, dead code, and unnecessary components."
        if role == "orchestrator":
            return f"Reported '{task.title}' status back to the main task: {task.status.value}."
        return f"{agent_name} considered '{task.title}' and recorded progress."


@dataclass
class OllamaBrain:
    model: str = os.environ.get("ORAC_MODEL", "llama3.2")
    endpoint: str = os.environ.get("ORAC_OLLAMA_URL", "http://localhost:11434/api/generate")
    timeout_seconds: int = 60

    def think(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        system_prompt = (
            f"You are {agent_name}, the {role.replace('_', ' ')} agent in ORAC. "
            "Be concise, concrete, and write only the work log entry."
        )
        payload = {
            "model": self.model,
            "prompt": f"{system_prompt}\n\nTask: {task.title}\nDescription: {task.description}\n\n{prompt}",
            "stream": False,
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc
        return str(data.get("response", "")).strip()


@dataclass
class OpenAICompatibleBrain:
    base_url: str
    model: str
    api_key: str | None = None
    timeout_seconds: int = 60

    def think(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        return self._complete(self._messages(agent_name, role, task, prompt))

    def think_json(
        self, agent_name: str, role: str, task: Task, prompt: str, schema: dict
    ) -> str:
        """Structured output: the server enforces the schema at the token level
        (LM Studio / OpenAI response_format), so the reply is valid JSON by
        construction for any capable model."""
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "decision", "strict": True, "schema": schema},
        }
        return self._complete(
            self._messages(agent_name, role, task, prompt), response_format
        )

    def _messages(self, agent_name: str, role: str, task: Task, prompt: str) -> list[dict]:
        return [
            {
                "role": "system",
                "content": (
                    f"You are {agent_name}, the {role.replace('_', ' ')} agent in ORAC. "
                    "Write only the concise work-log entry."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task: {task.title}\nDescription: {task.description}\n\n{prompt}"
                ),
            },
        ]

    def _complete(self, messages: list[dict], response_format: dict | None = None) -> str:
        payload: dict = {"model": self.model, "messages": messages, "temperature": 0.2}
        if response_format is not None:
            payload["response_format"] = response_format
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"OpenAI-compatible request failed: {exc}") from exc
        usage = data.get("usage") or {}
        record_llm_usage(
            self.model,
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
        )
        choices = data.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        content = str(message.get("content") or "").strip()
        if not content:
            # Reasoning models (e.g. qwen3) under strict structured output place
            # the schema-constrained answer in reasoning_content and leave
            # content empty. Read it rather than return "" — an empty reply
            # looks like a failed call and trips the availability fallback.
            content = str(message.get("reasoning_content") or "").strip()
        return content


@dataclass
class LMStudioBrain(OpenAICompatibleBrain):
    base_url: str = field(
        default_factory=lambda: os.environ.get("ORAC_LMSTUDIO_URL", "http://localhost:1234/v1")
    )
    model: str = field(default_factory=lambda: os.environ.get("ORAC_LMSTUDIO_MODEL", "local-model"))


@dataclass
class FoundationBrain(OpenAICompatibleBrain):
    base_url: str = field(
        default_factory=lambda: os.environ.get("ORAC_FOUNDATION_URL", "https://api.openai.com/v1")
    )
    model: str = field(default_factory=lambda: os.environ.get("ORAC_FOUNDATION_MODEL", "gpt-4.1-mini"))
    api_key: str | None = field(default_factory=lambda: os.environ.get("ORAC_FOUNDATION_API_KEY"))


@dataclass
class FallbackBrain:
    primary: Brain
    fallback: Brain

    def think(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        try:
            response = self.primary.think(agent_name, role, task, prompt)
        except RuntimeError as exc:
            task.add_log("system", f"Primary brain unavailable, using rules brain: {exc}")
            return self.fallback.think(agent_name, role, task, prompt)
        return response or self.fallback.think(agent_name, role, task, prompt)

    def think_json(
        self, agent_name: str, role: str, task: Task, prompt: str, schema: dict
    ) -> str:
        """Delegate structured output to whichever layer supports it.

        Availability fallback only (primary down -> next layer); a layer with
        no structured support uses plain think(), and the caller's strict
        parser still decides whether the reply stands.
        """
        primary_json = getattr(self.primary, "think_json", None)
        try:
            if callable(primary_json):
                response = primary_json(agent_name, role, task, prompt, schema)
            else:
                response = self.primary.think(agent_name, role, task, prompt)
        except RuntimeError as exc:
            task.add_log("system", f"Primary brain unavailable, using fallback: {exc}")
            return self._fallback_json(agent_name, role, task, prompt, schema)
        return response or self._fallback_json(agent_name, role, task, prompt, schema)

    def _fallback_json(
        self, agent_name: str, role: str, task: Task, prompt: str, schema: dict
    ) -> str:
        fallback_json = getattr(self.fallback, "think_json", None)
        if callable(fallback_json):
            return fallback_json(agent_name, role, task, prompt, schema)
        return self.fallback.think(agent_name, role, task, prompt)


def build_brain(name: str, model: str | None = None) -> Brain:
    if name == "rules":
        return RulesBrain()
    if name == "ollama":
        return FallbackBrain(primary=OllamaBrain(), fallback=RulesBrain())
    if name == "lmstudio":
        brain = LMStudioBrain()
        if model and model not in {"local", "small_local"}:
            brain.model = model
        return FallbackBrain(primary=brain, fallback=RulesBrain())
    if name == "foundation":
        foundation = FoundationBrain()
        if model and model != "foundation":
            foundation.model = model
        return FallbackBrain(
            primary=foundation,
            fallback=FallbackBrain(primary=LMStudioBrain(), fallback=RulesBrain()),
        )
    if name == "browser":
        from orac.browser_brain import BrowserFoundationBrain  # noqa: PLC0415

        provider = model if model and model not in {"browser", ""} else None
        brain = BrowserFoundationBrain(**({"provider": provider} if provider else {}))
        return FallbackBrain(
            primary=brain,
            fallback=FallbackBrain(primary=LMStudioBrain(), fallback=RulesBrain()),
        )
    raise ValueError(
        f"Unknown brain {name!r}. Expected 'rules', 'ollama', 'lmstudio', 'foundation', or 'browser'."
    )
