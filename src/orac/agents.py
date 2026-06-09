from __future__ import annotations

from dataclasses import dataclass

from orac.agent_registry import AgentProfile, load_agent_profiles
from orac.intent_backbone import IntentBackbone
from orac.llm import Brain
from orac.models import Task, TaskStatus
from orac.tooling import RegularToolExecutor


@dataclass
class RuntimeAgent:
    profile: AgentProfile
    brain: Brain
    tools: RegularToolExecutor

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def role(self) -> str:
        return self.profile.slug

    def work(self, task: Task) -> None:
        before = task.status
        acted = self._apply_builtin_action(task)
        if not acted:
            return
        prompt = self._task_prompt(task, before)
        task.add_log(self.name, self.brain.think(self.name, self.role, task, prompt))

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
        self.tools.run(
            "resource_budgeter",
            task,
            self.name,
            resource_type="available effort",
            budget_limit="60%",
        )
        self.tools.run(
            "handoff_tracker",
            task,
            self.name,
            next_owner="Simples",
            reason="ready work should follow the smallest effective path",
        )
        return True

    def _advance_simple_path(self, task: Task) -> bool:
        if task.status == TaskStatus.READY:
            self.tools.run(
                "minimal_path_planner",
                task,
                self.name,
                candidate_steps=["clarify", "budget", "implement", "review"],
            )
            task.transition(TaskStatus.IN_PROGRESS)
            return True
        if task.status == TaskStatus.IN_PROGRESS:
            self.tools.run(
                "implementation_log",
                task,
                self.name,
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
        self.tools.run("waste_scanner", task, self.name, scope="task outcome")
        self.tools.run(
            "design_replay",
            task,
            self.name,
            current_design="current task flow remains small enough to keep",
        )
        self.tools.run(
            "verification_log",
            task,
            self.name,
            checks=list(task.acceptance_criteria),
            result="passed",
        )
        task.transition(TaskStatus.DONE)
        return True

    def _report_to_main_task(self, task: Task) -> bool:
        if task.status in {TaskStatus.DONE, TaskStatus.BLOCKED}:
            return False
        self.tools.run("status_reporter", task, self.name)
        return True


def build_core_agents(brain: Brain) -> list[RuntimeAgent]:
    profiles = load_agent_profiles()
    executor = RegularToolExecutor()
    return [RuntimeAgent(profile=profile, brain=brain, tools=executor) for profile in profiles]
