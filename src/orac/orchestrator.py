from __future__ import annotations

from typing import Any

from orac.agent_registry import load_agent_profiles
from orac.agent_session import parse_decision
from orac.broker_store import MAX_SUBAGENTS, BrokerStore
from orac.llm import Brain
from orac.models import Task

# The Orchestrator's decomposition step — where a goal is broken into a fan-out
# of sub-intents. This is the home of the deliberate "abundance frame": the
# Orchestrator is told the *live* number of free subagent slots so it reasons
# "I have many workers, I should decompose" — a prompt-level bias toward
# breaking work down rather than cramming it into one monolithic agent.
#
# The frame is kept HONEST: the number comes from the register's live free-count,
# never a hardcoded string, and equals the same cap the register enforces. As the
# roster fills, the number drops and the frame self-tightens — the Orchestrator
# naturally decomposes less when the system is already loaded, with no throttle
# logic in the prompt. Over-decomposition is pruned by the Simple/Efficiency
# plan-review (the counterweight), not by lying about the budget.

DECOMPOSITION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "slices": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sub_intent": {"type": "string"},
                    "goal": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["sub_intent", "goal"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["slices"],
    "additionalProperties": False,
}

_PLAN_PROTOCOL = """\
Reply with ONE JSON object and nothing else:
{"slices": [{"sub_intent": "<one slice of the goal>", "goal": "<concrete work>",
             "acceptance_criteria": ["<checkable>", "..."]}, ...]}
The slices' sub_intents together must COVER the full intent — no gap. Each slice
must be independently doable and verifiable by one subagent."""


def abundance_frame(free: int, cap: int) -> str:
    """The honest abundance frame, built from the live free-slot count."""
    return (
        f"You can spawn subagents to do this work — you have {free} of {cap} "
        "subagent slots free right now. That is a large budget: prefer to "
        "DECOMPOSE the goal into several small, independent slices, each a clear "
        "sub-intent one subagent can own and verify, rather than doing everything "
        "in one monolithic agent. But every slice must earn its place — do not "
        "split work that is genuinely one step; the Simple and Efficiency lenses "
        "will prune needless fragmentation. Aim for the smallest decomposition "
        "that still fully covers the intent."
    )


def _orchestrator_persona() -> str:
    return {p.slug: p.system_prompt for p in load_agent_profiles()}["orchestrator"]


def propose_decomposition(
    goal: str,
    intent: str,
    store: BrokerStore,
    brain: Brain,
    *,
    cap: int = MAX_SUBAGENTS,
    task: Task | None = None,
) -> list[dict[str, Any]]:
    """Ask the Orchestrator to decompose ``goal`` into intent slices.

    The prompt carries the live free-slot count (the frame). Output is validated
    and fail-closed: an unparseable or empty plan raises, and a plan that exceeds
    the budget the model was honestly given raises too — the deterministic floor
    will not honour more slices than slots, so the frame is never a bluff.
    """
    free = store.subagent_free_slots(cap)
    prompt = (
        f"{_orchestrator_persona()}\n\n"
        f"{abundance_frame(free, cap)}\n\n"
        f"GOAL: {goal}\n"
        f"FULL INTENT TO COVER: {intent}\n\n"
        f"{_PLAN_PROTOCOL}\n\n"
        "Your decomposition:"
    )
    seed = task or Task(title="decompose", description=goal)
    think_json = getattr(brain, "think_json", None)
    if callable(think_json):
        reply = think_json("Orchestrator", "orchestrator", seed, prompt, DECOMPOSITION_SCHEMA)
    else:
        reply = brain.think("Orchestrator", "orchestrator", seed, prompt)

    decision = parse_decision(reply)
    if not decision or "slices" not in decision:
        raise ValueError(
            f"Orchestrator produced no parseable decomposition: {reply[:300]!r}"
        )
    raw = decision["slices"]
    if not isinstance(raw, list) or not raw:
        raise ValueError("Orchestrator decomposition has no slices.")

    slices: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict) or "sub_intent" not in entry:
            raise ValueError(f"Decomposition slice missing 'sub_intent': {entry!r}")
        slices.append(
            {
                "sub_intent": str(entry["sub_intent"]),
                "goal": str(entry.get("goal", entry["sub_intent"])),
                "acceptance_criteria": [str(c) for c in entry.get("acceptance_criteria", [])],
            }
        )

    if len(slices) > free:
        raise ValueError(
            f"Orchestrator proposed {len(slices)} slices but only {free} subagent "
            f"slot(s) are free; the plan exceeds its honest budget."
        )
    return slices
