from __future__ import annotations

from typing import Any

from orac.agent_registry import load_agent_profiles
from orac.lenses import _parse_json_object
from orac.llm import Brain
from orac.models import (
    CapabilityStatus,
    CouncilVerdict,
    LensDecision,
    LensVerdict,
    Task,
)

# (d) The counterweight to the abundance frame.
#
# The frame (orchestrator.py) deliberately biases the Orchestrator toward
# decomposing a lot. Left unchecked that produces sprawl — trivial work shattered
# into needless fragments. So before any subagent spawns, three lenses review the
# *plan itself* (a DISPATCH-edge review, distinct from the per-tool-call council):
#
#   Intent     — do the slices together COVER the full intent? gap/drift => escalate
#   Simple     — is this the MINIMAL decomposition? over-fragmentation => escalate
#   Efficiency — is there WASTE? overlapping/duplicate/off-goal slices => escalate
#
# Aggregation matches the council: any BLOCK => rejected; any ESCALATE => needs a
# human; else the plan proceeds. This is model judgment (the deterministic floor
# can only check structure, not semantic coverage), enforced deterministically.

PLAN_REVIEW_LENSES: tuple[tuple[str, str], ...] = (
    ("Intent", "intent"),
    ("Simple", "simples"),
    ("Efficiency", "efficiency"),
)

_LENS_FOCUS: dict[str, str] = {
    "Intent": (
        "Do the slices' sub_intents TOGETHER fully cover the full intent, with no "
        "gap and no drift beyond it? A missing piece or a slice that serves "
        "something other than the goal is a real problem."
    ),
    "Simple": (
        "Is this the MINIMAL decomposition that still covers the intent? Slices "
        "that are really one step, or splitting trivial work, is over-"
        "fragmentation — a real problem in your domain."
    ),
    "Efficiency": (
        "Is there WASTE across the slices — two slices doing the same thing, "
        "overlap, or a slice outside the goal? That is a real problem."
    ),
}

PLAN_REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["pass", "block", "escalate"]},
        "reason": {"type": "string"},
    },
    "required": ["decision", "reason"],
    "additionalProperties": False,
}

_PROTOCOL = """\
You are ONE review lens on a PROPOSED DECOMPOSITION of a goal into subagent
slices. Judge ONLY through your own lens's focus stated below, not what other
lenses own. Most reasonable plans pass — do not invent problems — but never
rubber-stamp a plan whose problem you can name.

Reply with ONE JSON object and nothing else:
{"decision": "pass|escalate|block", "reason": "<one short sentence>"}"""


def _personas() -> dict[str, str]:
    return {p.slug: p.system_prompt for p in load_agent_profiles()}


def _slices_block(slices: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"  {i + 1}. sub_intent: {s['sub_intent']}  | goal: {s.get('goal', '')}"
        for i, s in enumerate(slices)
    )


def review_decomposition(
    intent: str,
    slices: list[dict[str, Any]],
    brain: Brain,
    *,
    task: Task | None = None,
) -> CouncilVerdict:
    """Run the three plan-review lenses over a decomposition and aggregate.

    Requires a structured-output brain (no silent fall-through): a lens asked to
    judge must be able to. An unparseable verdict becomes ESCALATE — a visible
    "cannot judge", never a silent pass — exactly as the per-edge lenses do.
    """
    think_json = getattr(brain, "think_json", None)
    if not callable(think_json):
        raise RuntimeError(
            "Plan review requires a brain with structured output (think_json)."
        )
    personas = _personas()
    seed = task or Task(title="plan-review", description=intent)
    verdicts: list[LensVerdict] = []
    for name, slug in PLAN_REVIEW_LENSES:
        prompt = (
            f"{personas[slug]}\n\n"
            f"{_PROTOCOL}\n\n"
            f"YOUR LENS ({name}) FOCUS: {_LENS_FOCUS[name]}\n\n"
            f"FULL INTENT: {intent}\n"
            f"PROPOSED SLICES ({len(slices)}):\n{_slices_block(slices)}\n\n"
            f"Your verdict as the {name} lens:"
        )
        reply = think_json(name, slug, seed, prompt, PLAN_REVIEW_SCHEMA)
        decision, reason = _parse(reply)
        verdicts.append(LensVerdict(lens=name, decision=decision, reason=f"{name}: {reason}"))

    lenses = tuple(verdicts)
    blocks = [v for v in verdicts if v.decision is LensDecision.BLOCK]
    escalations = [v for v in verdicts if v.decision is LensDecision.ESCALATE]
    if blocks:
        return CouncilVerdict(
            status=CapabilityStatus.DENIED,
            lenses=lenses,
            reason="; ".join(v.reason for v in blocks),
        )
    if escalations:
        return CouncilVerdict(
            status=CapabilityStatus.PENDING,
            lenses=lenses,
            reason="; ".join(v.reason for v in escalations),
        )
    return CouncilVerdict(
        status=CapabilityStatus.ALLOWED,
        lenses=lenses,
        reason="plan review: all lenses pass",
    )


def _parse(reply: str) -> tuple[LensDecision, str]:
    decision = _parse_json_object(reply)
    if decision is None or decision.get("decision") not in {"pass", "block", "escalate"}:
        return LensDecision.ESCALATE, f"could not produce a usable verdict: {reply[:160]!r}"
    return LensDecision(str(decision["decision"])), str(decision.get("reason", "")).strip()


# The RETURN edge: a council review of what a slice actually delivered, on top of
# the deterministic verifier (tests/app). The verifier proves the work RUNS; these
# lenses judge whether what came back is on-goal, minimally shaped, and free of
# waste before it integrates — the semantic check passing tests cannot give.

_RETURN_LENSES: tuple[tuple[str, str], ...] = (
    ("Intent", "intent"),
    ("Simple", "simples"),
    ("Efficiency", "efficiency"),
)

_RETURN_FOCUS: dict[str, str] = {
    "Intent": (
        "Does the returned work actually serve THIS slice's goal, with no drift — "
        "did it solve the asked problem rather than a nearby different one, and "
        "cover the acceptance criteria? Solving the wrong thing is a real problem."
    ),
    "Simple": (
        "Is the result the minimal, plainest shape that meets the goal? Needless "
        "abstraction, indirection, or moving parts added for this slice is a real "
        "problem in your domain."
    ),
    "Efficiency": (
        "Is there WASTE in what was returned — dead code, duplicated logic, "
        "unnecessary ceremony, or scope beyond the goal? That is a real problem."
    ),
}

_RETURN_PROTOCOL = """\
You are ONE review lens on the WORK A SUBAGENT RETURNED for a slice (its done
claim already passed an independent verifier — tests run green). Judge ONLY
through your own lens's focus below. Most verified work passes — do not invent
problems — but never rubber-stamp returned work whose problem you can name.

Reply with ONE JSON object and nothing else:
{"decision": "pass|escalate|block", "reason": "<one short sentence>"}"""


def review_return(
    goal: str,
    acceptance_criteria: tuple[str, ...],
    evidence: str,
    brain: Brain,
    *,
    task: Task | None = None,
) -> CouncilVerdict:
    """Run the three lenses over a slice's RETURNED work and aggregate.

    The deterministic verifier (run_tests / verify_local_app) has already passed;
    this is the semantic gate before integration. Same contract and aggregation as
    the council (block -> denied, escalate -> pending, else allowed); an unparseable
    verdict is ESCALATE, never a silent pass. Requires a structured-output brain.
    """
    think_json = getattr(brain, "think_json", None)
    if not callable(think_json):
        raise RuntimeError(
            "Return review requires a brain with structured output (think_json)."
        )
    personas = _personas()
    seed = task or Task(title="return-review", description=goal)
    criteria = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  - (none given)"
    verdicts: list[LensVerdict] = []
    for name, slug in _RETURN_LENSES:
        prompt = (
            f"{personas[slug]}\n\n"
            f"{_RETURN_PROTOCOL}\n\n"
            f"YOUR LENS ({name}) FOCUS: {_RETURN_FOCUS[name]}\n\n"
            f"SLICE GOAL: {goal}\n"
            f"ACCEPTANCE CRITERIA:\n{criteria}\n"
            f"RETURNED EVIDENCE:\n{evidence[:1500]}\n\n"
            f"Your verdict as the {name} lens:"
        )
        reply = think_json(name, slug, seed, prompt, PLAN_REVIEW_SCHEMA)
        decision, reason = _parse(reply)
        verdicts.append(LensVerdict(lens=name, decision=decision, reason=f"{name}: {reason}"))

    lenses = tuple(verdicts)
    blocks = [v for v in verdicts if v.decision is LensDecision.BLOCK]
    escalations = [v for v in verdicts if v.decision is LensDecision.ESCALATE]
    if blocks:
        return CouncilVerdict(
            status=CapabilityStatus.DENIED, lenses=lenses,
            reason="; ".join(v.reason for v in blocks),
        )
    if escalations:
        return CouncilVerdict(
            status=CapabilityStatus.PENDING, lenses=lenses,
            reason="; ".join(v.reason for v in escalations),
        )
    return CouncilVerdict(
        status=CapabilityStatus.ALLOWED, lenses=lenses,
        reason="return review: all lenses pass",
    )
