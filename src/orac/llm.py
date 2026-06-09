from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from orac.models import Task


class Brain(Protocol):
    def think(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        ...


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
        payload = {
            "model": self.model,
            "messages": [
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
            ],
            "temperature": 0.2,
        }
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
        choices = data.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        return str(message.get("content", "")).strip()


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
    raise ValueError(
        f"Unknown brain {name!r}. Expected 'rules', 'ollama', 'lmstudio', or 'foundation'."
    )
