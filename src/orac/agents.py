from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orac.agent_registry import AgentProfile, load_agent_profiles
from orac.broker import ToolBroker
from orac.intent_backbone import IntentBackbone
from orac.llm import Brain
from orac.models import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    Task,
    TaskStatus,
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
        before = task.status
        try:
            acted = self._apply_builtin_action(task)
        except ApprovalPending as parked:
            task.park_for_approval(parked.pending_id, before)
            task.add_log(
                self.name,
                f"Parked for approval (pending {parked.pending_id}).",
            )
            return
        if not acted:
            return
        prompt = self._task_prompt(task, before)
        task.add_log(self.name, self.brain.think(self.name, self.role, task, prompt))

    def _use(self, task: Task, tool: str, **args: Any) -> CapabilityResult:
        """Route one tool call through the broker.

        The deterministic flow relies on each grant being in place, so a denied
        or errored capability is a wiring bug, not a soft path — fail loud.
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

    def _task_prompt(self, task: Task, previous_status: TaskStatus) -> str:
        tools = ", ".join(self.profile.tools)
        criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria) or "- None yet"
        return (
            f"{self.profile.system_prompt}\n\n"
            f"Available regular tools: {tools}\n"
            f"Previous status: {previous_status.value}\n"
            f"Current status: {task.status.value}\n"
            f"Acceptance criteria:\n{criteria}\n\n"
            "Write the next concise work-log entry for this task."
        )

    def _apply_builtin_action(self, task: Task) -> bool:
        if self.profile.slug == "intent":
            return self._clarify_intent(task)
        if self.profile.slug == "optimiser":
            return self._budget_resources(task)
        if self.profile.slug == "simples":
            return self._advance_simple_path(task)
        if self.profile.slug == "efficiency":
            return self._review_efficiency(task)
        if self.profile.slug == "orchestrator":
            return self._report_to_main_task(task)
        return False

    def _clarify_intent(self, task: Task) -> bool:
        return IntentBackbone().apply_gate(task, self.name)

    def _budget_resources(self, task: Task) -> bool:
        if task.status != TaskStatus.READY:
            return False
        self._use(
            task,
            "resource_budgeter",
            resource_type="available effort",
            budget_limit="60%",
        )
        self._use(
            task,
            "handoff_tracker",
            next_owner="Simples",
            reason="ready work should follow the smallest effective path",
        )
        return True

    def _advance_simple_path(self, task: Task) -> bool:
        if task.status == TaskStatus.READY:
            self._use(
                task,
                "minimal_path_planner",
                candidate_steps=["clarify", "budget", "implement", "review"],
            )
            task.transition(TaskStatus.IN_PROGRESS)
            return True
        if task.status == TaskStatus.IN_PROGRESS:
            self._use(
                task,
                "implementation_log",
                change_summary="advanced the task to review using the current minimal path",
            )
            task.transition(TaskStatus.REVIEW)
            return True
        return False

    def _review_efficiency(self, task: Task) -> bool:
        if task.status != TaskStatus.REVIEW:
            return False
        if not task.acceptance_criteria:
            task.add_log(self.name, "Blocked: no acceptance criteria available for review.")
            task.transition(TaskStatus.BLOCKED)
            return True
        self._use(task, "waste_scanner", scope="task outcome")
        self._use(
            task,
            "design_replay",
            current_design="current task flow remains small enough to keep",
        )
        self._use(
            task,
            "verification_log",
            checks=list(task.acceptance_criteria),
            result="passed",
        )
        task.transition(TaskStatus.DONE)
        return True

    def _report_to_main_task(self, task: Task) -> bool:
        if task.status in {TaskStatus.DONE, TaskStatus.BLOCKED}:
            return False
        self._use(task, "status_reporter")
        return True


def build_core_agents(
    brain: Brain, broker: ToolBroker | None = None
) -> list[RuntimeAgent]:
    profiles = load_agent_profiles()
    broker = broker or ToolBroker.from_manifests()
    return [RuntimeAgent(profile=profile, brain=brain, broker=broker) for profile in profiles]
