from __future__ import annotations

from dataclasses import dataclass, field

from orac.intent_backbone import IntentBackbone
from orac.models import Task, TaskStatus

# The intent gate — the pre-work front door.
#
# Exactly one path advances a task on the intent axis: this gate. It holds a
# task in CLARIFYING, surfacing one question (or the echo check) at a time,
# until intent is locked — then it fixes goal + acceptance_criteria + work_kind
# and releases the task to READY for its kind's doer session.
#
# The council's IntentLens (council.py::_intent) shares the name but does a
# different job: it is a during-execution broker-edge check that blocks action
# on a closed task. It never moves a task's status. Gate = before work; lens =
# during work.

# work_kind classification — keyword -> category. "code" is the residual: it is
# ORAC's bootstrap capability and the only kind with a doer today, so a task
# that names no other category is code work (mirrors driver.py's documented
# default). This is a classification policy, not a silent fallback — every task
# gets a definite kind, recorded on the task and visible on the board.
_KIND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "comms": ("email", "message", "notify", "slack", "reply", "draft", "send", "announce"),
    "media": ("image", "video", "audio", "render", "picture", "artwork", "media", "thumbnail"),
    "physical": ("device", "light", "switch", "motor", "robot", "actuator", "gpio", "relay", "thermostat"),
    "event": ("session", "game", "workshop", "facilitate", "tournament", "host a", "run a round"),
}


def classify_work_kind(task: Task) -> str:
    text = f"{task.title} {task.description}".lower()
    for kind, words in _KIND_KEYWORDS.items():
        if any(word in text for word in words):
            return kind
    return "code"


@dataclass
class IntentGate:
    """Owns the clarification loop and the release to READY."""

    backbone: IntentBackbone = field(default_factory=IntentBackbone)
    agent_name: str = "Intent"

    OWNED_STATUSES = frozenset({TaskStatus.BACKLOG, TaskStatus.CLARIFYING})

    def advance(self, task: Task) -> bool:
        """One gate tick. Returns True if it touched the task.

        Locked + confident -> release to READY. Otherwise the task stays in
        CLARIFYING and the next unanswered question (or the echo check, once all
        fields are answered) is logged for the operator to answer out of band
        via the existing status flow.
        """
        if task.status not in self.OWNED_STATUSES:
            return False
        assessment = self.backbone.assess(task)
        if assessment.locked and assessment.confidence >= self.backbone.confidence_threshold:
            self.release(task)
            return True
        task.transition(TaskStatus.CLARIFYING)
        if assessment.next_question:
            task.add_log(
                self.agent_name,
                f"Clarify loop at {assessment.confidence}% confidence. "
                f"Next question: {assessment.next_question}",
            )
        else:
            task.add_log(
                self.agent_name,
                f"Echo check ready at {assessment.confidence}% confidence: {assessment.echo_check}",
            )
        return True

    def release(self, task: Task) -> None:
        """Lock the work order and move the task to READY: fix acceptance
        criteria, goal, and work_kind. Idempotent on fields already set — the
        driver pre-fills goal + work_kind from its standing mandate, then calls
        release() to get the same READY task a human earns by answering."""
        if not task.acceptance_criteria:
            self.backbone.apply_acceptance_criteria(task)
        task.metadata.setdefault("goal", task.description or task.title)
        if task.work_kind is None:
            task.work_kind = classify_work_kind(task)
        task.transition(TaskStatus.READY)
        task.add_log(self.agent_name, "Intent locked. Work may proceed to build and self-test.")
