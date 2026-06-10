from __future__ import annotations

from dataclasses import dataclass

from orac.broker_store import BrokerStore

# P6 notify transport — making the review queue *reach* the operator instead of
# waiting to be polled. The queue itself (notifications + pending approvals) is
# durable in the BrokerStore; this module turns its current state into a single
# operator-facing signal that the daemon prints each tick and the UI surfaces in
# its state payload. A real push channel (Windows toast, etc.) is one more
# transport that consumes the same summary.


@dataclass(frozen=True)
class ReviewQueueSummary:
    """The review queue's current pressure, in one operator-facing shape.

    ``unacked`` — completed actions awaiting retrospective review (review-after).
    ``pending`` — actions parked for approval before they may run (ask-before).
    """

    unacked_notifications: int
    pending_approvals: int

    @property
    def total(self) -> int:
        return self.unacked_notifications + self.pending_approvals

    @property
    def is_clear(self) -> bool:
        return self.total == 0

    def message(self) -> str:
        if self.is_clear:
            return "Review queue clear."
        parts: list[str] = []
        if self.pending_approvals:
            parts.append(
                f"{self.pending_approvals} pending approval(s) — `orac reviews` then "
                "`orac approve`/`deny`"
            )
        if self.unacked_notifications:
            parts.append(
                f"{self.unacked_notifications} action(s) awaiting review — `orac reviews` then "
                "`orac ack`/`rollback`"
            )
        return "Review queue: " + "; ".join(parts)

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "unacked_notifications": self.unacked_notifications,
            "pending_approvals": self.pending_approvals,
            "total": self.total,
            "is_clear": self.is_clear,
        }


def review_queue_summary(store: BrokerStore) -> ReviewQueueSummary:
    return ReviewQueueSummary(
        unacked_notifications=len(store.list_notifications(unacked_only=True)),
        pending_approvals=len(store.list_pending()),
    )
