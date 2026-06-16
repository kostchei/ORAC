from __future__ import annotations

from collections import Counter
from typing import Any

from orac.broker_store import BrokerStore

# Gap E: a read-only aggregation over the broker's persisted audit + reviews +
# queue. Pure derivation — no writes, no hot-loop cost — computed on demand. This
# is the self-tuning surface the roadmap's soak run wants: which lenses fire and
# how often, how often calls are denied/escalated, and how deep the review queue
# is. It feeds lens calibration (`orac lenses eval`) and, later, empirical
# decomposition sizing (gap G).


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
    return "\n".join(lines)
