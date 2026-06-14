from __future__ import annotations

from typing import Any

from orac.models import Task

# The intent ledger — how a parent goal's intent is carried faithfully across a
# fan-out of subagents, and how the system refuses to call the parent done until
# every slice of that intent is satisfied (design: "ensure the full intent gets
# carried; remind the Orchestrator his job is not finished").
#
# The ledger lives on the parent Task's metadata so it survives a board
# save/load round-trip. It is deterministic bookkeeping: it cannot *prove* that
# the declared slices semantically cover the parent intent (that judgment is
# Intent's plan-review), but it CAN guarantee that no declared slice is dropped
# and that the parent stays open until all of them close. That floor is airtight.

_LEDGER_KEY = "intent_decomposition"

# The slice-contract fields carried alongside the bookkeeping ones, so the full
# contract (one owner, named verifier, return evidence) survives the parent's
# board save/load round-trip and is available to the child and the broker's
# contract-scope enforcement — not just {sub_intent, goal, acceptance_criteria}.
_SLICE_EXTRA_KEYS = (
    "work_kind",
    "inputs",
    "allowed_tools",
    "forbidden_tools",
    "owned_paths_or_resources",
    "verifier",
    "risk_class",
    "budget",
    "expected_artifact",
    "return_evidence",
    "integration_note",
)

# A slice is one piece of the decomposed intent, mapped to one child task.
SLICE_OPEN = "open"
SLICE_SATISFIED = "satisfied"
SLICE_BLOCKED = "blocked"


def open_ledger(parent: Task, intent: str, slices: list[dict[str, Any]]) -> None:
    """Record the decomposition of ``intent`` into ``slices`` on the parent.

    Each slice is ``{"sub_intent": str, "goal": str, "acceptance_criteria": [...]}``.
    Opening a ledger twice on the same task is refused — a decomposition is
    declared once; re-opening would silently discard tracked progress.
    """
    if not slices:
        raise ValueError("Cannot open an intent ledger with no slices.")
    if has_ledger(parent):
        raise ValueError(f"Task {parent.id} already has an intent ledger.")
    parent.metadata[_LEDGER_KEY] = {
        "intent": intent,
        "slices": [_ledger_entry(s) for s in slices],
    }


def _ledger_entry(s: dict[str, Any]) -> dict[str, Any]:
    """One ledger slice: the bookkeeping fields plus any contract fields present."""
    entry: dict[str, Any] = {
        "sub_intent": str(s["sub_intent"]),
        "goal": str(s.get("goal", s["sub_intent"])),
        "acceptance_criteria": [str(c) for c in s.get("acceptance_criteria", [])],
        "child_id": None,
        "status": SLICE_OPEN,
    }
    for key in _SLICE_EXTRA_KEYS:
        if key in s:
            entry[key] = s[key]
    return entry


def has_ledger(parent: Task) -> bool:
    return _LEDGER_KEY in parent.metadata


def _ledger(parent: Task) -> dict[str, Any]:
    if not has_ledger(parent):
        raise ValueError(f"Task {parent.id} has no intent ledger.")
    return parent.metadata[_LEDGER_KEY]


def slices(parent: Task) -> list[dict[str, Any]]:
    return list(_ledger(parent)["slices"])


def attach_child(parent: Task, index: int, child_id: str) -> None:
    """Bind the slice at ``index`` to the child task executing it."""
    _ledger(parent)["slices"][index]["child_id"] = child_id


def mark(parent: Task, child_id: str, status: str) -> None:
    """Set the status of the slice owned by ``child_id``.

    A satisfied slice should only be marked after the child's done-means is
    verified (callers pass the result of ``verify_goal_done``), so the ledger
    never records intent as met on an unverified claim.
    """
    if status not in {SLICE_OPEN, SLICE_SATISFIED, SLICE_BLOCKED}:
        raise ValueError(f"Invalid slice status {status!r}.")
    for entry in _ledger(parent)["slices"]:
        if entry["child_id"] == child_id:
            entry["status"] = status
            return
    raise KeyError(f"No ledger slice owned by child {child_id!r} on task {parent.id}.")


def unsatisfied(parent: Task) -> list[dict[str, Any]]:
    """Slices not yet satisfied (open or blocked) — the work still owed."""
    return [s for s in _ledger(parent)["slices"] if s["status"] != SLICE_SATISFIED]


def is_covered(parent: Task) -> bool:
    """True iff every declared slice of the intent is satisfied."""
    return all(s["status"] == SLICE_SATISFIED for s in _ledger(parent)["slices"])


def is_blocked(parent: Task) -> bool:
    """True if any slice is blocked — the intent cannot be fully covered as-is."""
    return any(s["status"] == SLICE_BLOCKED for s in _ledger(parent)["slices"])


def coverage_report(parent: Task) -> str:
    """One-line operator-facing status, also the Orchestrator's reminder."""
    entries = _ledger(parent)["slices"]
    satisfied = sum(1 for s in entries if s["status"] == SLICE_SATISFIED)
    total = len(entries)
    if is_covered(parent):
        return f"intent fully covered ({satisfied}/{total} slices satisfied)"
    pending = [
        f"{s['sub_intent']} [{s['status']}]"
        for s in entries
        if s["status"] != SLICE_SATISFIED
    ]
    return (
        f"intent NOT yet covered ({satisfied}/{total} satisfied); "
        f"still owed: {'; '.join(pending)}"
    )
