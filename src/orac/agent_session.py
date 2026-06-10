from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from orac.agent_registry import AgentProfile, get_tool_map
from orac.broker import ToolBroker
from orac.llm import Brain
from orac.models import CapabilityRequest, CapabilityStatus, Task

# The agent loop — where the model actually chooses.
#
# A session is a fresh context with a single contract (the user's stated
# method: "new context window with single task"). Each turn the model reasons
# over the contract plus the transcript of its own actions and observations,
# and emits exactly one structured decision:
#
#   {"tool": "...", "args": {...}}     use a capability (the broker adjudicates)
#   {"done": true, "summary": "..."}   contract satisfied; only this crosses back
#   {"blocked": true, "reason": "..."} cannot proceed; says why
#
# Autonomy of means, by construction not trust: whatever the model emits goes
# through the broker — grants, council floor, risk model, audit. A denial is an
# observation the model can adapt to, not a crash.

OBSERVATION_LIMIT = 1500
DEFAULT_MAX_STEPS = 16

# Enforced server-side where the brain supports structured output (LM Studio /
# OpenAI response_format): the model physically cannot emit a malformed
# decision. Brains without the capability get plain think() and the strict
# parser below remains the gate.
DECISION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "args": {"type": "object"},
        "done": {"type": "boolean"},
        "summary": {"type": "string"},
        "blocked": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
}

PROTOCOL = """\
RESPONSE PROTOCOL — reply with a single JSON object and nothing else:
  {"tool": "<tool name>", "args": {...}}   to use one of your tools
  {"done": true, "summary": "<what you produced and how it was verified>"}
  {"blocked": true, "reason": "<what stops you>"}
Use one tool per reply. Tool results appear as OBSERVATION lines. If a tool is
denied or fails, adapt; do not repeat the identical call."""


@dataclass(frozen=True)
class SessionResult:
    status: str  # "done" | "blocked" | "pending"
    summary: str
    steps: int
    pending_id: int | None = None


@dataclass
class AgentSession:
    profile: AgentProfile
    brain: Brain
    broker: ToolBroker
    max_steps: int = DEFAULT_MAX_STEPS
    transcript: list[str] = field(default_factory=list)

    def run(self, task: Task, contract: str) -> SessionResult:
        think_json = getattr(self.brain, "think_json", None)
        for step in range(1, self.max_steps + 1):
            if callable(think_json):
                reply = think_json(
                    self.profile.name,
                    self.profile.slug,
                    task,
                    self._prompt(contract),
                    DECISION_SCHEMA,
                )
            else:
                reply = self.brain.think(
                    self.profile.name, self.profile.slug, task, self._prompt(contract)
                )
            decision = parse_decision(reply)
            if decision is None:
                return self._finish(
                    task,
                    SessionResult(
                        status="blocked",
                        summary=f"Unparseable model reply at step {step}: {reply[:200]!r}",
                        steps=step,
                    ),
                )

            if decision.get("done"):
                return self._finish(
                    task,
                    SessionResult(
                        status="done", summary=str(decision.get("summary", "")), steps=step
                    ),
                )
            if decision.get("blocked"):
                return self._finish(
                    task,
                    SessionResult(
                        status="blocked", summary=str(decision.get("reason", "")), steps=step
                    ),
                )

            tool = decision.get("tool")
            if not tool:
                return self._finish(
                    task,
                    SessionResult(
                        status="blocked",
                        summary=f"Decision named no tool at step {step}: {decision!r}",
                        steps=step,
                    ),
                )
            args = decision.get("args") or {}
            try:
                result = self.broker.request(
                    CapabilityRequest(
                        agent=self.profile.name, tool=str(tool), task_id=task.id, args=dict(args)
                    ),
                    task,
                )
            except Exception as exc:  # noqa: BLE001 — a tool fault is feedback, not a crash
                # An adapter that raises (bad path, missing file, git error) is an
                # observation the model adapts to, not a loop-killing crash. The
                # session stays bounded by max_steps.
                self.transcript.append(f"ACTION {step}: {tool} {json.dumps(dict(args))[:400]}")
                self.transcript.append(f"OBSERVATION {step} [error]: {type(exc).__name__}: {exc}")
                continue
            if result.status is CapabilityStatus.PENDING:
                return SessionResult(
                    status="pending",
                    summary=result.message,
                    steps=step,
                    pending_id=int(result.data["pending_id"]),
                )
            self.transcript.append(f"ACTION {step}: {tool} {json.dumps(dict(args))[:400]}")
            self.transcript.append(
                f"OBSERVATION {step} [{result.status.value}]: "
                f"{result.message} {json.dumps(result.data, default=str)[:OBSERVATION_LIMIT]}"
            )

        return self._finish(
            task,
            SessionResult(
                status="blocked",
                summary=f"Step budget exhausted ({self.max_steps}) without done/blocked.",
                steps=self.max_steps,
            ),
        )

    def _prompt(self, contract: str) -> str:
        tools = get_tool_map()
        specs = "\n".join(
            f"- {name}: {tools[name].description} (inputs: {', '.join(tools[name].inputs)})"
            for name in self.profile.tools
            if name in tools
        )
        history = "\n".join(self.transcript) if self.transcript else "(no actions yet)"
        return (
            f"{self.profile.system_prompt}\n\n"
            f"{PROTOCOL}\n\n"
            f"YOUR TOOLS:\n{specs}\n\n"
            f"CONTRACT:\n{contract}\n\n"
            f"TRANSCRIPT:\n{history}\n\n"
            "Your next JSON decision:"
        )

    def _finish(self, task: Task, result: SessionResult) -> SessionResult:
        task.add_log(
            self.profile.name,
            f"Session {result.status} after {result.steps} step(s): {result.summary}",
        )
        return result


def parse_decision(reply: str) -> dict[str, Any] | None:
    """Parse the model's JSON decision; None if it is not a JSON object.

    Tolerates markdown code fences (formatting), nothing else (content).
    """
    text = reply.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        decision = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(decision, dict):
        return None
    return decision
