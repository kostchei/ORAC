from __future__ import annotations

from collections import Counter
from typing import Any

from orac.broker_store import MAX_SUBAGENTS, BrokerStore
from orac.self_tune import DECOMPOSE_THRESHOLD_DEFAULT, DECOMPOSE_THRESHOLD_KEY

# A read-only aggregation over the broker's persisted audit + reviews + queue +
# roster. Pure derivation — no writes, no hot-loop cost, computed on demand. This
# is the observability surface the soak run wants (which lenses fire, how often
# calls are denied/escalated, how deep the review queue is) and the feedback
# surface the self-tuning loop reads (see self_tune.py).
#
# Deliberately broker-only: every signal here comes from BrokerStore, so the
# rollup is a pure SQL derivation with no board-file I/O and no fallbacks. The
# board-derived signal the tuner needs (goal done/blocked outcomes) lives in
# self_tune.py, where the daemon already holds the loaded board.


def compute_metrics(store: BrokerStore) -> dict[str, Any]:
    """Roll up the persisted governance signal into a machine-readable summary."""
    audit = store.audit_log()
    by_status: Counter[str] = Counter(e.status for e in audit)
    by_tool: Counter[str] = Counter(e.tool for e in audit)

    reviews = store.list_reviews()
    by_lens: dict[str, Counter[str]] = {}
    for row in reviews:
        by_lens.setdefault(row["lens"], Counter())[row["decision"]] += 1

    pending = store.list_pending()
    unacked = store.list_notifications(unacked_only=True)
    roster_used = store.subagent_roster_count()
    threshold = int(
        store.get_tunable(DECOMPOSE_THRESHOLD_KEY, str(DECOMPOSE_THRESHOLD_DEFAULT))
    )

    return {
        "audit": {
            "total": len(audit),
            "by_status": dict(by_status),
            "by_tool": dict(by_tool.most_common()),
        },
        "reviews": {
            # The reviews table holds only non-clean verdicts (a clean pass is not
            # persisted), so these counts are escalations/blocks by lens.
            "total": len(reviews),
            "by_lens": {lens: dict(counts) for lens, counts in by_lens.items()},
        },
        "queue": {
            "pending_approvals": len(pending),
            "unacked_notifications": len(unacked),
        },
        "roster": {
            "in_use": roster_used,
            "cap": MAX_SUBAGENTS,
        },
        "tuning": {
            # The current value of the one self-tuned knob, so `orac metrics`
            # shows where the loop has settled.
            "decompose_points_threshold": threshold,
        },
    }


def render_metrics(m: dict[str, Any]) -> str:
    """Human-readable form of ``compute_metrics`` for the CLI."""
    audit = m["audit"]
    lines = [
        "ORAC metrics",
        f"  audit: {audit['total']} brokered call(s); by status: {audit['by_status'] or {}}",
    ]
    if audit["by_tool"]:
        top = list(audit["by_tool"].items())[:8]
        lines.append("  top tools: " + ", ".join(f"{tool}={n}" for tool, n in top))
    reviews = m["reviews"]
    lines.append(f"  reviews (non-clean verdicts): {reviews['total']}")
    for lens, decisions in sorted(reviews["by_lens"].items()):
        breakdown = ", ".join(f"{d}={n}" for d, n in sorted(decisions.items()))
        lines.append(f"    {lens}: {breakdown}")
    queue = m["queue"]
    lines.append(
        f"  queue: {queue['pending_approvals']} pending approval(s), "
        f"{queue['unacked_notifications']} unacked notification(s)"
    )
    roster = m["roster"]
    lines.append(f"  roster: {roster['in_use']}/{roster['cap']} slots in use")
    lines.append(
        f"  tuning: decompose threshold = points > "
        f"{m['tuning']['decompose_points_threshold']}"
    )
    return "\n".join(lines)
