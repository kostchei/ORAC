from __future__ import annotations

from enum import StrEnum
from pathlib import Path
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
    "repo.edit_file": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    "git.create_branch": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    "git.commit": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    # Pushing publishes to a remote others may pull from: hard to reverse, external.
    "git.push": RiskClass(Reversibility.HARD, Externality.EXTERNAL_PRIVATE),
    # Revert creates a new commit undoing a previous one: the rollback primitive.
    "git.revert": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    # Stash push/pop set aside and restore uncommitted work: local, reversible.
    "git.stash": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    "git.stash_pop": RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
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
# Fully enumerated on purpose — no computed fallback.
#
# Review-after, not ask-before (user policy): code work must never block the
# loop. Checkpoint-first writes, commits, and pushes run unattended and land in
# the review queue ("I did X, here is the working result — ok? rollback
# available"). APPROVE is reserved for the genuinely irreversible/external:
# communications, financial, physical.
_THROTTLE: dict[tuple[Reversibility, Externality], ApprovalMode] = {
    (Reversibility.REVERSIBLE, Externality.LOCAL): ApprovalMode.AUTO,
    (Reversibility.REVERSIBLE, Externality.EXTERNAL_PRIVATE): ApprovalMode.NOTIFY,
    (Reversibility.REVERSIBLE, Externality.EXTERNAL_PUBLIC): ApprovalMode.NOTIFY,
    (Reversibility.REVERSIBLE, Externality.FINANCIAL): ApprovalMode.APPROVE,
    (Reversibility.REVERSIBLE, Externality.PHYSICAL): ApprovalMode.NOTIFY,
    (Reversibility.HARD, Externality.LOCAL): ApprovalMode.NOTIFY,
    (Reversibility.HARD, Externality.EXTERNAL_PRIVATE): ApprovalMode.NOTIFY,
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


# --- safety-critical-file gate (design §8.7) --------------------------------
#
# The files that *enforce* the safety model are a different class from ordinary
# code. Editing them is the system rewriting its own governor or its own
# privilege boundary (§4.6 grant seed). A loop that can weaken its own brakes —
# or grant itself new powers — must not run that change under auto+notify, even
# though it is reversible by checkpoint. Such an edit escalates to a human
# regardless of reversibility; the council's deterministic floor reads this
# predicate and turns a match into an ESCALATE (the existing park/approve path).
#
# Paths are repo-root-relative, POSIX-style. Matching is suffix-on-boundary so
# both the relative form the agent names ("src/orac/policy.py") and the absolute
# form the adapter resolves it to ("D:/Code/ORAC/src/orac/policy.py") match,
# while a lookalike ("notsrc/orac/policy.py") does not.
SAFETY_CRITICAL_PATHS: frozenset[str] = frozenset(
    {
        "src/orac/broker.py",        # the broker: the single enforcement edge
        "src/orac/broker_store.py",  # durable grants/audit/pending/notifications
        "src/orac/policy.py",        # the risk model + this gate itself
        "src/orac/council.py",       # the deterministic review floor
        "src/orac/lenses.py",        # the LLM cognition layer
        "src/orac/scrum.py",         # the loop that parks/resumes on approval
        "src/orac/daemon.py",        # the 24/7 driver wiring
        "src/orac/agent_session.py", # the agent loop the broker adjudicates
        "src/orac/prompts/agents.json",  # the grant seed: who-can-write (§4.6)
    }
)

# Tool -> the arg key holding the path(s) it would write/commit. A safety-critical
# match on any of these escalates. Reads/searches/status are not listed: only a
# mutation of the governor is gated, not looking at it.
_PATH_BEARING_TOOLS: dict[str, str] = {
    "repo.write_file": "path",   # whole-file mutation
    "repo.edit_file": "path",    # surgical mutation
    "git.commit": "paths",       # making the change durable (list of paths)
}


def _normalise(raw: str) -> str:
    return Path(raw).as_posix()


def _matches_critical(raw: str) -> bool:
    posix = _normalise(raw)
    return any(
        posix == critical or posix.endswith("/" + critical)
        for critical in SAFETY_CRITICAL_PATHS
    )


def safety_critical_paths_touched(
    tool: str, args: dict[str, Any] | None
) -> list[str]:
    """Return the safety-critical path(s) a write/commit would touch, else [].

    Deterministic and store-free: it needs only the request, so it works on the
    no-DB path too. The council's Sentinel lens calls this; a non-empty result
    means the edge escalates to a human (design §8.7).
    """
    key = _PATH_BEARING_TOOLS.get(tool)
    if key is None or not args:
        return []
    raw = args.get(key)
    if raw is None:
        return []
    candidates = raw if isinstance(raw, (list, tuple)) else [raw]
    return [str(p) for p in candidates if _matches_critical(str(p))]
