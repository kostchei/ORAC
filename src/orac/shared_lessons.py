from __future__ import annotations

from typing import TYPE_CHECKING

from orac.models import TaskStatus

if TYPE_CHECKING:
    from orac.broker_store import BrokerStore
    from orac.models import Task

# Shared lessons: a per-goal, cross-agent scratchpad (the DeLM "shared verified
# context" idea, scoped to one fan-out). Sibling slices of the same goal — and
# repeated attempts at the same slice — read each other's *verified* outcomes
# before they start and contribute their own after they settle, so the fan-out
# stops rediscovering the same dead ends.
#
# Two deliberate design choices:
#   - It is deterministic, not an LLM tool. Reads are injected into the contract
#     and writes are recorded from the verified slice outcome, so a doer cannot
#     forget to share or write to the wrong scope.
#   - "Verified" is literal: a lesson is only written AFTER the slice's verifier
#     (and, in the full fan-out, the RETURN council review) has settled its
#     status, so the scratchpad never carries an unproven claim forward.

MAX_LESSONS_INJECTED = 12  # cap so a long fan-out cannot bloat a doer's contract
_LESSON_TEXT_CAP = 280     # keep each note compact (DeLM: "compact typed notes")

_KIND_RESULT = "result"
_KIND_FAILURE = "failure"


def _label(task: "Task") -> str:
    return (task.title or task.description or task.id).strip()[:80]


def _latest_detail(task: "Task") -> str:
    """The most informative recent line from the slice's work log."""
    if not task.work_log:
        return ""
    return task.work_log[-1].message.strip()


def record_slice_outcome(store: "BrokerStore", scope: str, child: "Task") -> None:
    """Write a verified lesson from a settled slice. No-op for non-terminal slices.

    A done slice contributes what worked; a blocked slice contributes the dead
    end so peers (and any repair attempt) do not repeat it. A slice still parked
    for approval has no verified outcome yet, so nothing is written.
    """
    if child.status is TaskStatus.DONE:
        kind = _KIND_RESULT
    elif child.status is TaskStatus.BLOCKED:
        kind = _KIND_FAILURE
    else:
        return
    detail = _latest_detail(child)
    text = f"{_label(child)}: {child.status.value}"
    if detail:
        text = f"{text} — {detail}"
    store.record_lesson(scope, kind, text[:_LESSON_TEXT_CAP])


def render_for_contract(store: "BrokerStore", scope: str) -> str:
    """Render a goal's shared lessons as a contract block, or '' if there are none."""
    lessons = store.lessons_for(scope, limit=MAX_LESSONS_INJECTED)
    if not lessons:
        return ""
    bullets = []
    for lesson in lessons:
        marker = "OK" if lesson.kind == _KIND_RESULT else "AVOID"
        bullets.append(f"- [{marker}] {lesson.text}")
    return "\n".join(bullets)
