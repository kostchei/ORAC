from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orac.intent_backbone import IntentBackbone, IntentField
from orac.models import Task


@dataclass
class ToolResult:
    name: str
    message: str
    data: dict[str, Any]


class RegularToolExecutor:
    def run(self, name: str, task: Task, agent: str, **kwargs: Any) -> ToolResult:
        handlers = {
            "task_reader": self.task_reader,
            "intent_silent_scan": self.intent_silent_scan,
            "clarification_question": self.clarification_question,
            "echo_check": self.echo_check,
            "intent_lock": self.intent_lock,
            "intent_reset": self.intent_reset,
            "acceptance_criteria_editor": self.acceptance_criteria_editor,
            "assumption_log": self.assumption_log,
            "resource_budgeter": self.resource_budgeter,
            "capacity_checker": self.capacity_checker,
            "risk_register": self.risk_register,
            "minimal_path_planner": self.minimal_path_planner,
            "implementation_log": self.implementation_log,
            "waste_scanner": self.waste_scanner,
            "design_replay": self.design_replay,
            "verification_log": self.verification_log,
            "status_reporter": self.status_reporter,
            "handoff_tracker": self.handoff_tracker,
        }
        try:
            handler = handlers[name]
        except KeyError as exc:
            raise ValueError(f"Unknown regular tool {name!r}.") from exc
        return handler(task=task, agent=agent, **kwargs)

    def intent_silent_scan(self, task: Task, agent: str) -> ToolResult:
        assessment = IntentBackbone().assess(task)
        missing = [field.value for field in assessment.missing_fields]
        message = f"Silent scan complete: confidence {assessment.confidence}%, missing {missing}."
        task.add_log(agent, message)
        return ToolResult(
            "intent_silent_scan",
            message,
            {"confidence": assessment.confidence, "missing_fields": missing},
        )

    def clarification_question(
        self, task: Task, agent: str, missing_field: str
    ) -> ToolResult:
        field = IntentField(missing_field)
        question = IntentBackbone().assess(task).next_question
        message = f"Clarification question for {field.value}: {question}"
        task.add_log(agent, message)
        return ToolResult("clarification_question", message, {"question": question})

    def echo_check(self, task: Task, agent: str) -> ToolResult:
        echo = IntentBackbone().echo_check(task)
        message = f"Echo check: {echo}"
        task.add_log(agent, message)
        return ToolResult("echo_check", message, {"echo_check": echo})

    def intent_lock(self, task: Task, agent: str) -> ToolResult:
        assessment = IntentBackbone().lock(task)
        message = f"Intent locked at {assessment.confidence}% confidence."
        task.add_log(agent, message)
        return ToolResult("intent_lock", message, {"confidence": assessment.confidence})

    def intent_reset(self, task: Task, agent: str) -> ToolResult:
        IntentBackbone().reset(task)
        message = "Intent reset."
        task.add_log(agent, message)
        return ToolResult("intent_reset", message, {})

    def task_reader(self, task: Task, agent: str) -> ToolResult:
        del agent
        return ToolResult(
            name="task_reader",
            message="Task read.",
            data={
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "status": task.status.value,
                "assignee": task.assignee,
                "acceptance_criteria": list(task.acceptance_criteria),
            },
        )

    def acceptance_criteria_editor(
        self, task: Task, agent: str, criteria: list[str]
    ) -> ToolResult:
        task.acceptance_criteria = list(criteria)
        message = f"Acceptance criteria set: {len(criteria)} item(s)."
        task.add_log(agent, message)
        return ToolResult("acceptance_criteria_editor", message, {"criteria": criteria})

    def assumption_log(self, task: Task, agent: str, assumption: str, impact: str) -> ToolResult:
        message = f"Assumption recorded: {assumption} Impact: {impact}"
        task.add_log(agent, message)
        return ToolResult("assumption_log", message, {"assumption": assumption, "impact": impact})

    def resource_budgeter(
        self, task: Task, agent: str, resource_type: str, budget_limit: str
    ) -> ToolResult:
        message = f"Resource budget set for {resource_type}: {budget_limit}."
        task.add_log(agent, message)
        return ToolResult(
            "resource_budgeter",
            message,
            {"resource_type": resource_type, "budget_limit": budget_limit},
        )

    def capacity_checker(
        self, task: Task, agent: str, available_capacity: int
    ) -> ToolResult:
        fits = task.points <= available_capacity
        message = f"Capacity check: {task.points}/{available_capacity} point(s), fits={fits}."
        task.add_log(agent, message)
        return ToolResult("capacity_checker", message, {"fits": fits})

    def risk_register(self, task: Task, agent: str, risk: str, mitigation: str) -> ToolResult:
        message = f"Risk recorded: {risk} Mitigation: {mitigation}"
        task.add_log(agent, message)
        return ToolResult("risk_register", message, {"risk": risk, "mitigation": mitigation})

    def minimal_path_planner(
        self, task: Task, agent: str, candidate_steps: list[str]
    ) -> ToolResult:
        message = "Minimal path selected: " + " -> ".join(candidate_steps)
        task.add_log(agent, message)
        return ToolResult("minimal_path_planner", message, {"candidate_steps": candidate_steps})

    def implementation_log(self, task: Task, agent: str, change_summary: str) -> ToolResult:
        message = f"Implementation note: {change_summary}"
        task.add_log(agent, message)
        return ToolResult("implementation_log", message, {"change_summary": change_summary})

    def waste_scanner(self, task: Task, agent: str, scope: str) -> ToolResult:
        message = f"Waste scan complete for {scope}: no unnecessary component recorded."
        task.add_log(agent, message)
        return ToolResult("waste_scanner", message, {"scope": scope})

    def design_replay(self, task: Task, agent: str, current_design: str) -> ToolResult:
        message = f"Design replay complete: {current_design}"
        task.add_log(agent, message)
        return ToolResult("design_replay", message, {"current_design": current_design})

    def verification_log(self, task: Task, agent: str, checks: list[str], result: str) -> ToolResult:
        message = f"Verification {result}: " + "; ".join(checks)
        task.add_log(agent, message)
        return ToolResult("verification_log", message, {"checks": checks, "result": result})

    def status_reporter(self, task: Task, agent: str) -> ToolResult:
        message = (
            f"Main task report: {task.id} is {task.status.value}; "
            f"next owner is {task.assignee or 'unassigned'}."
        )
        task.add_log(agent, message)
        return ToolResult("status_reporter", message, {"status": task.status.value})

    def handoff_tracker(self, task: Task, agent: str, next_owner: str, reason: str) -> ToolResult:
        task.assignee = next_owner
        message = f"Handoff to {next_owner}: {reason}"
        task.add_log(agent, message)
        return ToolResult("handoff_tracker", message, {"next_owner": next_owner, "reason": reason})
