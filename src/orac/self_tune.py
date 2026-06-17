from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from orac.models import CapabilityRequest, CapabilityResult, CapabilityStatus, now_iso

if TYPE_CHECKING:  # avoid an import cycle: broker_store imports nothing from here
    from orac.broker_store import BrokerStore
    from orac.models import Board

# The self-tuning loop (Gap E). A closed loop that reads observed outcomes and
# adjusts ONE governing parameter — how eagerly the daemon fans a goal out into
# subagents (the points threshold in scrum._should_decompose) — within hard
# bounds, auto-applying and recording the change for retrospective human review
# (review-after, not ask-before).
#
# Safety boundary (the reason this is allowed to auto-apply): the knob is a pure
# *performance* knob. The loop can only move the decomposition threshold inside
# [MIN, MAX]; it cannot touch grants, risk classes, lens thresholds, or any part
# of the safety floor. The worst it can do is fan work out more or less eagerly,
# and every change lands in the notification queue where a human can see it and
# set the tunable back by hand. It never makes the system MORE aggressive than
# the hand-set default — it backs off under stress and recovers toward baseline.

DECOMPOSE_THRESHOLD_KEY = "decompose_points_threshold"
DECOMPOSE_THRESHOLD_DEFAULT = 1  # scrum decomposes when task.points > threshold
_TUNED_AT_KEY = "decompose_points_threshold_tuned_at"

# Hard bounds. MIN is the hand-set default (the loop never gets more aggressive
# than the designer chose); MAX caps how conservative it may become.
THRESHOLD_MIN = 1
THRESHOLD_MAX = 4

# Control law thresholds on the goal blocked-rate.
BLOCKED_RATE_HIGH = 0.5   # too much fan-out is blocking -> back off (raise threshold)
BLOCKED_RATE_LOW = 0.1    # healthy -> recover toward baseline (lower threshold)
MIN_SAMPLES = 5           # need enough finished goals before trusting the rate
COOLDOWN_SECONDS = 3600   # at most one change per hour, to damp oscillation


@dataclass(frozen=True)
class TuningAdjustment:
    key: str
    old: int
    new: int
    reason: str


def goal_outcomes(board: "Board") -> tuple[int, int]:
    """Count finished goal tasks as (done, blocked).

    A "goal" is a top-level work item (it carries a work_kind and the ``goal``
    metadata the daemon sets), not a council bookkeeping task. Only terminal
    states count toward the rate; in-flight tasks are ignored.
    """
    from orac.models import TaskStatus  # local import: avoid import cycle at module load

    done = blocked = 0
    for task in board.tasks:
        if task.work_kind is None or "goal" not in task.metadata:
            continue
        if task.status == TaskStatus.DONE:
            done += 1
        elif task.status == TaskStatus.BLOCKED:
            blocked += 1
    return done, blocked


def propose_threshold(
    current: int, done: int, blocked: int, *, min_samples: int = MIN_SAMPLES
) -> tuple[int, str] | None:
    """Decide the next threshold from goal outcomes, or None to hold. Pure.

    Returns ``(new_threshold, reason)`` only when a bounded change is warranted:
    - blocked-rate high -> raise the threshold (decompose less eagerly), and
    - blocked-rate low  -> lower it back toward the baseline (decompose more),
    each clamped to [MIN, MAX]. Too few samples, or a rate in the dead band,
    holds.
    """
    samples = done + blocked
    if samples < min_samples:
        return None
    rate = blocked / samples
    if rate >= BLOCKED_RATE_HIGH and current < THRESHOLD_MAX:
        new = current + 1
        return new, (
            f"goal blocked-rate {rate:.0%} ({blocked}/{samples}) over "
            f"{BLOCKED_RATE_HIGH:.0%}: fan out less eagerly (threshold {current}->{new})"
        )
    if rate <= BLOCKED_RATE_LOW and current > THRESHOLD_MIN:
        new = current - 1
        return new, (
            f"goal blocked-rate {rate:.0%} ({blocked}/{samples}) under "
            f"{BLOCKED_RATE_LOW:.0%}: recover toward baseline (threshold {current}->{new})"
        )
    return None


def _cooldown_active(store: "BrokerStore", cooldown_seconds: int) -> bool:
    last = store.get_tunable(_TUNED_AT_KEY, "")
    if not last:
        return False
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
    return elapsed < cooldown_seconds


def maybe_self_tune(
    store: "BrokerStore",
    board: "Board",
    *,
    min_samples: int = MIN_SAMPLES,
    cooldown_seconds: int = COOLDOWN_SECONDS,
) -> TuningAdjustment | None:
    """Read outcomes, adjust the decomposition threshold within bounds, notify.

    Auto-applies a single bounded step and records it to the notification queue
    ("I changed X because Y — ok?"). Returns the adjustment made, or None when it
    held (too few samples, dead band, or still in cooldown).
    """
    if _cooldown_active(store, cooldown_seconds):
        return None
    current = int(store.get_tunable(DECOMPOSE_THRESHOLD_KEY, str(DECOMPOSE_THRESHOLD_DEFAULT)))
    done, blocked = goal_outcomes(board)
    proposal = propose_threshold(current, done, blocked, min_samples=min_samples)
    if proposal is None:
        return None
    new, reason = proposal
    store.set_tunable(DECOMPOSE_THRESHOLD_KEY, str(new))
    store.set_tunable(_TUNED_AT_KEY, now_iso())
    _notify(store, current, new, reason)
    return TuningAdjustment(DECOMPOSE_THRESHOLD_KEY, current, new, reason)


def _notify(store: "BrokerStore", old: int, new: int, reason: str) -> None:
    """Surface the auto-applied change in the review-after queue."""
    req = CapabilityRequest(
        agent="SelfTuner",
        tool="config.decompose_threshold",
        task_id="",
        args={"old": old, "new": new},
    )
    result = CapabilityResult(
        status=CapabilityStatus.ALLOWED,
        tool="config.decompose_threshold",
        message=f"Self-tuned decomposition threshold {old} -> {new}: {reason}",
        data={"old": old, "new": new, "reason": reason},
    )
    store.record_notification(req, result)
