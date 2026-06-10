from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orac.agent_registry import AgentProfile, load_agent_profiles
from orac.broker import ToolBroker
from orac.llm import Brain
from orac.models import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    Task,
)


class ApprovalPending(Exception):
    """Raised when a tool call needs human approval before it can run.

    Carries the durable pending-approval id so the loop can park the task and
    resume it once the approval is resolved.
    """

    def __init__(self, pending_id: int) -> None:
        super().__init__(f"Tool call parked for approval (pending {pending_id}).")
        self.pending_id = pending_id


@dataclass
class RuntimeAgent:
    """A capability-using actor: it routes tool calls through the broker and
    parks its task when a call needs human approval.

    The deterministic council once drove task *status* from here, marching
    tasks through a scripted READY -> IN_PROGRESS -> REVIEW -> DONE. That
    theatrical path is gone: real work happens in AgentSession, the intent axis
    advances only in IntentGate, and the only outcome the council shapes now is
    via broker edges. RuntimeAgent remains the thin broker-facing actor (e.g.
    the Builder using a granted tool, or a test exercising the park machinery).
    """

    profile: AgentProfile
    brain: Brain
    broker: ToolBroker

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def role(self) -> str:
        return self.profile.slug

    def work(self, task: Task) -> None:
        """Perform this agent's action, parking the task if the broker requires
        approval. ``_act`` supplies the concrete behaviour; the base agent has
        none — the live system drives real work through AgentSession."""
        before = task.status
        try:
            self._act(task)
        except ApprovalPending as parked:
            task.park_for_approval(parked.pending_id, before)
            task.add_log(self.name, f"Parked for approval (pending {parked.pending_id}).")

    def _act(self, task: Task) -> bool:
        """No built-in action. Overridden where an agent has concrete work."""
        del task
        return False

    def _use(self, task: Task, tool: str, **args: Any) -> CapabilityResult:
        """Route one tool call through the broker.

        A denied or errored capability is a wiring bug, not a soft path — fail
        loud. A pending verdict becomes an ApprovalPending the caller parks on.
        """
        result = self.broker.request(
            CapabilityRequest(agent=self.name, tool=tool, task_id=task.id, args=args),
            task,
        )
        if result.status is CapabilityStatus.PENDING:
            raise ApprovalPending(int(result.data["pending_id"]))
        if result.status is not CapabilityStatus.ALLOWED:
            raise RuntimeError(
                f"{self.name} could not use {tool!r}: "
                f"{result.status.value} - {result.message}"
            )
        return result


def build_core_agents(
    brain: Brain, broker: ToolBroker | None = None
) -> list[RuntimeAgent]:
    profiles = load_agent_profiles()
    broker = broker or ToolBroker.from_manifests()
    return [RuntimeAgent(profile=profile, brain=brain, broker=broker) for profile in profiles]
