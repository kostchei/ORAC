from __future__ import annotations

from enum import StrEnum
from typing import Any

from orac.models import Externality, Reversibility, RiskClass

# The risk model (design §4.4). Deterministic, no LLM: a tool+args is classified
# into a RiskClass, and the throttle table maps that class to how the broker must
# handle it. This runs inside broker._decide, downstream of the model's output —
# the model cannot ignore it, because the model never executes the action.


class ApprovalMode(StrEnum):
    AUTO = "auto"        # run immediately, audit only
    NOTIFY = "notify"    # run immediately, but tell the human (transport: P6)
    APPROVE = "approve"  # park as pending until a human approves


# Journaling tools mutate only the in-memory Task log: local and reversible by
# construction. Listed explicitly so a NEW journaling tool must be added here
# deliberately rather than defaulting in.
_JOURNALING_TOOLS = frozenset(
    {
        "intent_silent_scan",
        "clarification_question",
        "echo_check",
        "intent_lock",
        "intent_reset",
        "task_reader",
        "acceptance_criteria_editor",
        "assumption_log",
        "resource_budgeter",
        "capacity_checker",
        "risk_register",
        "minimal_path_planner",
        "implementation_log",
        "waste_scanner",
        "design_replay",
        "verification_log",
        "status_reporter",
        "handoff_tracker",
    }
)

# Real adapters touch the world; every one must be classified explicitly.
_ADAPTER_RISK: dict[str, RiskClass] = {
    "fs_read": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    "repo.read_file": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    "repo.search": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    "git.status": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    # Running tests executes repo code, but it is local and part of the trusted
    # build loop; treated as reversible-local rather than spamming notifications.
    "repo.run_tests": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    # Writes are reversible because the Builder works checkpoint-first (branch +
    # commit before changing files).
    "repo.write_file": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    "git.create_branch": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    "git.commit": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    # Pushing publishes to a remote others may pull from: hard to reverse, external.
    "git.push": RiskClass(Reversibility.HARD, Externality.EXTERNAL_PRIVATE),
}


def risk_class(tool: str, args: dict[str, Any] | None = None) -> RiskClass:
    """Classify a tool call. Unknown tools raise — no silent default.

    ``args`` is accepted for future arg-sensitive classification (e.g. a generic
    shell tool inspecting its command); unused today. An unclassified adapter must
    fail closed rather than run as ``auto``.
    """
    del args  # reserved for arg-sensitive classification
    if tool in _ADAPTER_RISK:
        return _ADAPTER_RISK[tool]
    if tool in _JOURNALING_TOOLS:
        return RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL)
    raise ValueError(
        f"No risk classification for tool {tool!r}; classify it in policy.py "
        "before it can run."
    )


# The throttle table: every (reversibility x externality) pair maps to a mode.
# Fully enumerated on purpose — no computed fallback. To make `git.push` notify
# instead of gate (the "push is fine, only comms ask" preference), change the
# single cell (HARD, EXTERNAL_PRIVATE) from APPROVE to NOTIFY.
_THROTTLE: dict[tuple[Reversibility, Externality], ApprovalMode] = {
    (Reversibility.REVERSIBLE, Externality.LOCAL): ApprovalMode.AUTO,
    (Reversibility.REVERSIBLE, Externality.EXTERNAL_PRIVATE): ApprovalMode.NOTIFY,
    (Reversibility.REVERSIBLE, Externality.EXTERNAL_PUBLIC): ApprovalMode.NOTIFY,
    (Reversibility.REVERSIBLE, Externality.FINANCIAL): ApprovalMode.APPROVE,
    (Reversibility.REVERSIBLE, Externality.PHYSICAL): ApprovalMode.NOTIFY,
    (Reversibility.HARD, Externality.LOCAL): ApprovalMode.NOTIFY,
    (Reversibility.HARD, Externality.EXTERNAL_PRIVATE): ApprovalMode.APPROVE,
    (Reversibility.HARD, Externality.EXTERNAL_PUBLIC): ApprovalMode.APPROVE,
    (Reversibility.HARD, Externality.FINANCIAL): ApprovalMode.APPROVE,
    (Reversibility.HARD, Externality.PHYSICAL): ApprovalMode.APPROVE,
    (Reversibility.IRREVERSIBLE, Externality.LOCAL): ApprovalMode.NOTIFY,
    (Reversibility.IRREVERSIBLE, Externality.EXTERNAL_PRIVATE): ApprovalMode.APPROVE,
    (Reversibility.IRREVERSIBLE, Externality.EXTERNAL_PUBLIC): ApprovalMode.APPROVE,
    (Reversibility.IRREVERSIBLE, Externality.FINANCIAL): ApprovalMode.APPROVE,
    (Reversibility.IRREVERSIBLE, Externality.PHYSICAL): ApprovalMode.APPROVE,
}


def approval_mode(risk: RiskClass) -> ApprovalMode:
    return _THROTTLE[(risk.reversibility, risk.externality)]


def approval_mode_for(tool: str, args: dict[str, Any] | None = None) -> ApprovalMode:
    """Convenience: classify then look up the mode in one call."""
    return approval_mode(risk_class(tool, args))
