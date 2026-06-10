from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orac.agent_session import parse_decision
from orac.broker_store import BrokerStore
from orac.council import today_utc
from orac.intent_backbone import IntentBackbone, IntentField
from orac.llm import Brain
from orac.models import Board, Task, TaskStatus

# Initiative — the one place in ORAC where work originates instead of being
# handed down (design §4.2.1/§4.2.3). When the board is idle, the Optimise
# driver reads the system's own telemetry and forms a self-improvement goal.
# The originated task then flows through the same machinery as any other:
# Builder builds it, the council floor checks it, the review queue surfaces it.
#
# Bounded on purpose: rate-capped per day, reversible-only mandate, and the
# goal must come back as parseable JSON or origination fails loudly (a visible
# BLOCKED task on the board, not a silent skip).

ORIGINATE_DAILY_CAP = 3
ORIGINATE_COUNTER = "originate_task"

ACTIVE_STATUSES = {
    TaskStatus.BACKLOG,
    TaskStatus.CLARIFYING,
    TaskStatus.READY,
    TaskStatus.IN_PROGRESS,
    TaskStatus.REVIEW,
    TaskStatus.PENDING_APPROVAL,
}

ORIGINATION_PROMPT = """\
You are the Optimise driver of ORAC. The board is idle and resources are free.
Your standing mandate: with no other work, ORAC investigates its own system —
testing, securing, and hardening itself. Propose ONE small, concrete,
reversible self-improvement goal.

WORK KINDS AVAILABLE: {kinds}
(kinds marked no-doer have no doer agent yet; proposing one creates a visibly
blocked task — only do that to flag genuinely needed non-code work)

SYSTEM TELEMETRY:
{telemetry}

Reply with a single JSON object and nothing else:
{{"goal": "<one concrete achievable goal>",
  "work_kind": "<one of the kinds above>",
  "why": "<which telemetry signal motivates it>",
  "acceptance_criteria": ["<checkable criterion>", "..."]}}
"""


@dataclass(frozen=True)
class Origination:
    task: Task
    goal: str
    why: str


def gather_telemetry(board: Board, store: BrokerStore, repo_root: str | Path) -> dict[str, Any]:
    """What the system knows about itself — the raw material of initiative."""
    by_status: dict[str, int] = {}
    for task in board.tasks:
        by_status[task.status.value] = by_status.get(task.status.value, 0) + 1
    escalations = [
        f"{r['lens']}:{r['tool']}({r['reason'][:80]})"
        for r in store.list_reviews()
        if r["decision"] in {"escalate", "block"}
    ][-10:]
    roadmap = Path(repo_root) / "docs" / "roadmap.md"
    open_items: list[str] = []
    if roadmap.is_file():
        open_items = [
            line.strip()[6:].strip()
            for line in roadmap.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- [ ]")
        ][:10]
    return {
        "tasks_by_status": by_status,
        "unacked_review_queue": len(store.list_notifications()),
        "open_pending_approvals": len(store.list_pending()),
        "recent_council_flags": escalations,
        "roadmap_open_items": open_items,
    }


def board_is_idle(board: Board) -> bool:
    return not any(task.status in ACTIVE_STATUSES for task in board.tasks)


def originate(
    board: Board,
    store: BrokerStore,
    brain: Brain,
    repo_root: str | Path,
    daily_cap: int = ORIGINATE_DAILY_CAP,
) -> Origination | None:
    """Form one self-improvement goal from telemetry and put it on the board.

    Returns None when origination is not warranted (board busy, or the daily
    cap is spent — the governor declining is normal operation, not an error).
    Malformed model output raises: a driver that silently fails to think is a
    fault to surface, not to skip.
    """
    if not board_is_idle(board):
        return None
    if store.rate_count("Optimiser", ORIGINATE_COUNTER, today_utc()) >= daily_cap:
        return None

    from orac.work import WORK_KINDS

    telemetry = gather_telemetry(board, store, repo_root)
    kinds = ", ".join(
        spec.kind if spec.doer_slug else f"{spec.kind} (no-doer)"
        for spec in WORK_KINDS.values()
    )
    seed = Task(title="originate self-improvement", description="driver tick")
    reply = brain.think(
        "Optimiser", "optimiser", seed, ORIGINATION_PROMPT.format(
            kinds=kinds, telemetry=json.dumps(telemetry, indent=2)
        )
    )
    decision = parse_decision(reply)
    if decision is None or "goal" not in decision:
        raise ValueError(f"Optimise driver produced no parseable goal: {reply[:300]!r}")

    goal = str(decision["goal"])
    work_kind = str(decision.get("work_kind", "code"))
    if work_kind not in WORK_KINDS:
        raise ValueError(f"Optimise driver named unknown work kind {work_kind!r}.")
    why = str(decision.get("why", ""))
    criteria = [str(c) for c in decision.get("acceptance_criteria", [])] or [
        "verifiable per the work kind's done-means"
    ]

    task = Task(
        title=goal,
        description=why or goal,
        work_kind=work_kind,
        metadata={"origin": "optimise-driver", "goal": goal},
    )
    _lock_intent_from_mandate(task, goal, why, criteria, work_kind)
    board.add_task(task)
    task.add_log(
        "Optimiser",
        f"Originated from idle telemetry: {why or 'standing self-improvement mandate'}",
    )
    store.bump_rate("Optimiser", ORIGINATE_COUNTER, today_utc())
    return Origination(task=task, goal=goal, why=why)


def _lock_intent_from_mandate(
    task: Task, goal: str, why: str, criteria: list[str], work_kind: str
) -> None:
    """Pre-answer the intent gate from the standing mandate.

    A human task earns READY by answering Intent's questions; a self-originated
    task answers them from the mandate that authorised it — reversible-only
    work verified per its kind's done-means — then locks.
    """
    from orac.work import WORK_KINDS

    intent = IntentBackbone()
    answers = {
        IntentField.PURPOSE: why or goal,
        IntentField.AUDIENCE: "the ORAC system and its operator",
        IntentField.MUST_INCLUDE: criteria[0],
        IntentField.SUCCESS_CRITERIA: "; ".join(criteria),
        IntentField.FORMAT: f"{work_kind} deliverable: {WORK_KINDS[work_kind].done_means}",
        IntentField.TECH_STACK: "current repository stack",
        IntentField.EDGE_CASES: "covered by the kind's verification",
        IntentField.RISK_TOLERANCE: "reversible-only, per the standing mandate",
    }
    for fieldname, value in answers.items():
        intent.answer(task, fieldname, value)
    intent.lock(task)
    task.acceptance_criteria = list(criteria)
