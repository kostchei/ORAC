from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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


# Default reservoir thresholds (precautionary, non-blocking)
RESERVOIR_COUNT_THRESHOLD_DEFAULT = 15
RESERVOIR_SPAN_MINUTES_DEFAULT = 45
RESERVOIR_IDLE_GAP_MINUTES_DEFAULT = 20


def _parse_iso(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


def _load_reservoir_config(root: Path | str | None) -> dict[str, Any]:
    if root is None:
        return {}
    config_path = Path(root) / ".orac" / "config.json"
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data.get("reservoir", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def reservoir_advisory(
    store: BrokerStore,
    now: datetime | None = None,
    count_threshold: int | None = None,
    span_minutes_threshold: int | None = None,
    idle_gap_minutes: int | None = None,
) -> str | None:
    """Evaluate consecutive human queue-clearing actions against fatigue thresholds.

    W1: Read-only (queries audit log for agent='human', never mutates).
    W2: Advisory-only (returns warning string or None, never blocks/gates).
    W3: Honesty discipline (reports observed counts and span minutes vs declared thresholds).
    """
    cfg = _load_reservoir_config(getattr(store, "root", None))
    c_thresh = count_threshold if count_threshold is not None else int(cfg.get("count_threshold", RESERVOIR_COUNT_THRESHOLD_DEFAULT))
    s_thresh = span_minutes_threshold if span_minutes_threshold is not None else int(cfg.get("span_minutes", RESERVOIR_SPAN_MINUTES_DEFAULT))
    gap_min = idle_gap_minutes if idle_gap_minutes is not None else int(cfg.get("idle_gap_minutes", RESERVOIR_IDLE_GAP_MINUTES_DEFAULT))

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    try:
        entries = store.human_audit_log(limit=200)
    except Exception:
        try:
            entries = [e for e in store.audit_log(limit=200) if e.agent == "human"]
        except Exception:
            return None

    if not entries:
        return None

    sitting_entries = []
    latest_dt = None
    for entry in entries:
        entry_dt = _parse_iso(entry.created_at)
        if latest_dt is None:
            if (now - entry_dt).total_seconds() > gap_min * 60:
                break
            latest_dt = entry_dt
            sitting_entries.append((entry, entry_dt))
        else:
            prev_dt = sitting_entries[-1][1]
            if (prev_dt - entry_dt).total_seconds() > gap_min * 60:
                break
            sitting_entries.append((entry, entry_dt))

    if not sitting_entries:
        return None

    count = len(sitting_entries)
    oldest_dt = sitting_entries[-1][1]
    newest_dt = sitting_entries[0][1]
    span_minutes = round((newest_dt - oldest_dt).total_seconds() / 60)

    if count >= c_thresh or span_minutes >= s_thresh:
        return (
            f"[reservoir advisory] {count} queue action(s) cleared in {span_minutes}m "
            f"(threshold: {c_thresh} actions / {s_thresh}m). "
            "Precaution: review quality degrades during extended queue clearing."
        )

    return None


def review_queue_summary(store: BrokerStore) -> ReviewQueueSummary:
    return ReviewQueueSummary(
        unacked_notifications=len(store.list_notifications(unacked_only=True)),
        pending_approvals=len(store.list_pending()),
    )
