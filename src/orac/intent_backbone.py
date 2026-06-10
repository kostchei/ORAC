from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from orac.agent_registry import AgentProtocolSpec, AgentProtocolStep, load_agent_protocol
from orac.models import Task, TaskStatus


class IntentField(StrEnum):
    PURPOSE = "purpose"
    AUDIENCE = "audience"
    MUST_INCLUDE = "must_include"
    SUCCESS_CRITERIA = "success_criteria"
    FORMAT = "format"
    TECH_STACK = "tech_stack"
    EDGE_CASES = "edge_cases"
    RISK_TOLERANCE = "risk_tolerance"


FIELD_QUESTIONS: dict[IntentField, str] = {
    IntentField.PURPOSE: "What is the main purpose of this task?",
    IntentField.AUDIENCE: "Who is the result for?",
    IntentField.MUST_INCLUDE: "What is the number one fact, behavior, or feature that must be included?",
    IntentField.SUCCESS_CRITERIA: "How will we know this is successful?",
    IntentField.FORMAT: "What format should the final deliverable take?",
    IntentField.TECH_STACK: "If this is code, what tech stack or constraints should be used?",
    IntentField.EDGE_CASES: "What edge cases must be handled?",
    IntentField.RISK_TOLERANCE: "What risk tolerance should guide tradeoffs?",
}


@dataclass(frozen=True)
class IntentAssessment:
    confidence: int
    missing_fields: list[IntentField]
    next_question: str | None
    echo_check: str
    locked: bool


IntentProtocolStep = AgentProtocolStep
IntentProtocolSpec = AgentProtocolSpec


SPEC = load_agent_protocol("intent_translator_max.json")
PROTOCOL = SPEC.steps


class IntentBackbone:
    confidence_threshold = 95

    def assess(self, task: Task) -> IntentAssessment:
        state = self._state(task)
        answers = self._answers(task)
        missing = [field for field in IntentField if not self._answer_for(field, task, answers)]
        confidence = self._confidence(task, missing)
        next_question = FIELD_QUESTIONS[missing[0]] if missing else None
        return IntentAssessment(
            confidence=confidence,
            missing_fields=missing,
            next_question=next_question,
            echo_check=self.echo_check(task),
            locked=bool(state.get("locked", False)),
        )

    def answer(self, task: Task, field: IntentField | str, value: str) -> IntentAssessment:
        field = IntentField(field)
        state = self._state(task)
        answers = dict(state.get("answers", {}))
        answers[field.value] = value.strip()
        state["answers"] = answers
        state["locked"] = False
        task.metadata["intent"] = state
        task.transition(TaskStatus.CLARIFYING)
        task.add_log("Intent", f"Intent answer recorded for {field.value}.")
        return self.assess(task)

    def lock(self, task: Task) -> IntentAssessment:
        assessment = self.assess(task)
        if assessment.confidence < self.confidence_threshold:
            missing = ", ".join(field.value for field in assessment.missing_fields)
            raise ValueError(f"Cannot lock intent below 95% confidence. Missing: {missing}")
        state = self._state(task)
        state["locked"] = True
        task.metadata["intent"] = state
        self.apply_acceptance_criteria(task)
        # The release to READY (and the goal + work_kind it fixes) is the
        # IntentGate's job — the single front door. lock() only records the
        # YES-GO; the gate turns it into a buildable goal task on its next tick.
        task.add_log("Intent", "YES-GO received. Intent locked; awaiting gate release.")
        return self.assess(task)

    def reset(self, task: Task) -> None:
        task.metadata.pop("intent", None)
        task.acceptance_criteria.clear()
        task.transition(TaskStatus.BACKLOG)
        task.add_log("Intent", "RESET received. Intent state cleared.")

    def blueprint(self, task: Task) -> list[str]:
        assessment = self.assess(task)
        return [
            f"Confirm work order: {assessment.echo_check}",
            "Choose the smallest interface or artifact that satisfies the success criteria.",
            "Build only after YES-GO, then self-test against the locked acceptance criteria.",
        ]

    def risk_report(self, task: Task) -> list[str]:
        return [
            "Logic risk: solving the stated task while missing the actual purpose.",
            "Scope risk: adding components that are not required for the locked outcome.",
            "Verification risk: shipping without checking every acceptance criterion.",
        ]

    def echo_check(self, task: Task) -> str:
        answers = self._answers(task)
        deliverable = self._answer_for(IntentField.FORMAT, task, answers) or task.title
        must_include = self._answer_for(IntentField.MUST_INCLUDE, task, answers) or "the core requested outcome"
        constraint = self._answer_for(IntentField.RISK_TOLERANCE, task, answers) or "avoid unapproved assumptions"
        return (
            f"Deliver {deliverable} with {must_include}, while respecting {constraint}. "
            "Reply YES to lock, EDITS to revise, BLUEPRINT for plan, or RISK for failure modes."
        )

    def apply_acceptance_criteria(self, task: Task) -> None:
        answers = self._answers(task)
        task.acceptance_criteria = [
            f"Purpose met: {self._answer_for(IntentField.PURPOSE, task, answers)}",
            f"Must include: {self._answer_for(IntentField.MUST_INCLUDE, task, answers)}",
            f"Success criteria: {self._answer_for(IntentField.SUCCESS_CRITERIA, task, answers)}",
            f"Format delivered: {self._answer_for(IntentField.FORMAT, task, answers)}",
        ]

    def _confidence(self, task: Task, missing: list[IntentField]) -> int:
        total = len(IntentField)
        answered = total - len(missing)
        base = round(answered / total * 100)
        if task.title and task.description:
            base = min(100, base + 5)
        return base

    def _answer_for(
        self, field: IntentField, task: Task, answers: dict[str, str]
    ) -> str | None:
        answer = answers.get(field.value)
        if answer:
            return answer
        if field == IntentField.PURPOSE and task.description:
            return task.description
        if field == IntentField.FORMAT and self._looks_like_code_task(task):
            return "code change"
        if field == IntentField.TECH_STACK and self._looks_like_code_task(task):
            return "current repository stack"
        return None

    def _looks_like_code_task(self, task: Task) -> bool:
        text = f"{task.title} {task.description}".lower()
        return any(word in text for word in ["code", "repo", "implement", "build", "cli", "test"])

    def _answers(self, task: Task) -> dict[str, str]:
        raw = self._state(task).get("answers", {})
        return {str(key): str(value) for key, value in raw.items()}

    def _state(self, task: Task) -> dict[str, Any]:
        state = task.metadata.get("intent")
        if not isinstance(state, dict):
            state = {}
            task.metadata["intent"] = state
        return state
