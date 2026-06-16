from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from orac.broker_store import BrokerStore
from orac.storage import BoardStore

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

    # 1. Rounds to done from board
    board_store = BoardStore(store.root)
    try:
        board = board_store.load()
        tasks = board.tasks
    except Exception:
        tasks = []

    tasks_by_parent: dict[str, list[Any]] = {}
    for t in tasks:
        if t.parent_id:
            tasks_by_parent.setdefault(t.parent_id, []).append(t)

    primary_goals = [
        t for t in tasks 
        if t.work_kind is not None and not ("A prior attempt failed verification" in t.metadata.get("contract", {}).get("goal", ""))
    ]

    def count_repairs(task: Any) -> int:
        count = 0
        children = tasks_by_parent.get(task.id, [])
        for child in children:
            if "A prior attempt failed verification" in child.metadata.get("contract", {}).get("goal", ""):
                count += 1
            count += count_repairs(child)
        return count

    rounds_to_done = {t.id: count_repairs(t) for t in primary_goals}

    # 2. RETURN scores (first-round and final)
    reviews_by_session: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in reviews:
        if r.get("tool") == "return_review":
            reviews_by_session[(r["task_id"], r["tool"], r["created_at"])].append(r)

    task_sessions = defaultdict(list)
    for (task_id, tool, created_at), rows in reviews_by_session.items():
        task_sessions[task_id].append((created_at, rows))

    first_round_scores = {}
    final_round_scores = {}
    
    lens_weights = {
        "Intent": 0.30,
        "Simple": 0.25,
        "Security": 0.25,
        "Efficiency": 0.20,
    }

    for task_id, sessions in task_sessions.items():
        sessions.sort(key=lambda x: x[0])
        
        def calc_score(rows: list[dict[str, Any]]) -> float | None:
            if not rows or any(row.get("score") is None for row in rows):
                return None
            total = sum(row["score"] * lens_weights.get(row["lens"], 0.0) for row in rows)
            return round(total, 2)

        first_score = calc_score(sessions[0][1])
        final_score = calc_score(sessions[-1][1])
        if first_score is not None:
            first_round_scores[task_id] = first_score
        if final_score is not None:
            final_round_scores[task_id] = final_score

    # 3. Tool repetition trips count from tasks work log
    repetition_trips = 0
    doer_claimed_done = 0
    verification_failures = 0
    for t in tasks:
        for log in t.work_log:
            msg = log.message
            if "Repetition limit (" in msg:
                repetition_trips += 1
            if "Session done after" in msg:
                doer_claimed_done += 1
            if "did not pass verification" in msg:
                verification_failures += 1
            elif "Not accepted:" in msg:
                if "RETURN review:" not in msg:
                    verification_failures += 1

    verification_failure_rate = 0.0
    if doer_claimed_done > 0:
        verification_failure_rate = round(verification_failures / doer_claimed_done, 4)

    return {
        "audit": {
            "total": len(audit),
            "by_status": dict(by_status),
            "by_tool": dict(by_tool.most_common()),
        },
        "reviews": {
            "total": len(reviews),
            "by_lens": {lens: dict(counts) for lens, counts in by_lens.items()},
        },
        "queue": {
            "pending_approvals": len(pending),
            "unacked_notifications": len(unacked),
        },
        "rounds_to_done": rounds_to_done,
        "first_round_scores": first_round_scores,
        "final_round_scores": final_round_scores,
        "tool_repetition_trips": repetition_trips,
        "verification_failure_rate": verification_failure_rate,
        "scope_violations": 0,
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

    rounds = m.get("rounds_to_done", {})
    if rounds:
        lines.append("  rounds-to-done:")
        for task_id, count in sorted(rounds.items()):
            lines.append(f"    task {task_id}: {count} round(s)")

    first_scores = m.get("first_round_scores", {})
    final_scores = m.get("final_round_scores", {})
    if first_scores or final_scores:
        lines.append("  RETURN scores (first vs final):")
        all_tasks = sorted(set(first_scores.keys()) | set(final_scores.keys()))
        for task_id in all_tasks:
            first = first_scores.get(task_id, "-")
            final = final_scores.get(task_id, "-")
            lines.append(f"    task {task_id}: first={first}, final={final}")

    trips = m.get("tool_repetition_trips", 0)
    lines.append(f"  tool repetition trips: {trips}")
    
    v_rate = m.get("verification_failure_rate", 0.0)
    lines.append(f"  verification-failure rate: {v_rate:.2%}")

    queue = m["queue"]
    lines.append(
        f"  queue: {queue['pending_approvals']} pending approval(s), "
        f"{queue['unacked_notifications']} unacked notification(s)"
    )
    return "\n".join(lines)
