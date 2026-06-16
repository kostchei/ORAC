from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from orac.models import CapabilityStatus, LensDecision, Task
from orac.plan_review import SHIP_THRESHOLD, review_decomposition, review_return_scored


@dataclass
class ScriptedJSONBrain:
    """A structured-output brain: think_json returns scripted JSON, in lens order."""

    replies: list[str]
    prompts: list[str] = field(default_factory=list)

    def think_json(self, agent: str, role: str, task: Task, prompt: str, schema: dict) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0)

    def think(self, agent: str, role: str, task: Task, prompt: str) -> str:
        raise AssertionError("plan review must use think_json")


@dataclass
class PlainBrain:
    def think(self, agent: str, role: str, task: Task, prompt: str) -> str:
        return "{}"


def _v(decision: str, reason: str = "because") -> str:
    return json.dumps({"decision": decision, "reason": reason})


_SLICES = [
    {"sub_intent": "part a", "goal": "do a"},
    {"sub_intent": "part b", "goal": "do b"},
]


def test_all_pass_allows_the_plan() -> None:
    brain = ScriptedJSONBrain([_v("pass"), _v("pass"), _v("pass")])

    verdict = review_decomposition("full intent", _SLICES, brain)

    assert verdict.status is CapabilityStatus.ALLOWED
    assert len(verdict.lenses) == 3


def test_simple_escalates_over_fragmentation() -> None:
    # Simple is the second lens; it flags the split as needless.
    brain = ScriptedJSONBrain([_v("pass"), _v("escalate", "this is really one step"), _v("pass")])

    verdict = review_decomposition("full intent", _SLICES, brain)

    assert verdict.status is CapabilityStatus.PENDING
    assert "really one step" in verdict.reason
    assert any(v.lens == "Simple" and v.decision.value == "escalate" for v in verdict.lenses)


def test_intent_blocks_a_plan_that_misses_the_goal() -> None:
    brain = ScriptedJSONBrain([_v("block", "slices do not cover the goal"), _v("pass"), _v("pass")])

    verdict = review_decomposition("full intent", _SLICES, brain)

    assert verdict.status is CapabilityStatus.DENIED
    assert "do not cover" in verdict.reason


def test_unparseable_lens_reply_escalates_not_passes() -> None:
    brain = ScriptedJSONBrain(["I am not sure how to judge this", _v("pass"), _v("pass")])

    verdict = review_decomposition("full intent", _SLICES, brain)

    assert verdict.status is CapabilityStatus.PENDING  # the unparseable lens escalated
    assert any(v.decision.value == "escalate" for v in verdict.lenses)


def test_prompt_carries_intent_and_every_slice() -> None:
    brain = ScriptedJSONBrain([_v("pass"), _v("pass"), _v("pass")])

    review_decomposition("deliver the whole thing", _SLICES, brain)

    intent_prompt = brain.prompts[0]
    assert "deliver the whole thing" in intent_prompt
    assert "part a" in intent_prompt and "part b" in intent_prompt


def test_plan_review_requires_structured_output() -> None:
    with pytest.raises(RuntimeError, match="structured output"):
        review_decomposition("intent", _SLICES, PlainBrain())


# --- gap A: scored + Security RETURN review ----------------------------------

def _s(decision: str, score: int, reason: str = "because") -> str:
    return json.dumps({"decision": decision, "score": score, "reason": reason})


def _scored_review(replies: list[str]):
    # Lens order: Intent, Simple, Security, Efficiency.
    return review_return_scored(
        "add a helper", ("works",), "the returned code", ScriptedJSONBrain(replies)
    )


def test_scored_review_ships_when_all_pass_above_threshold() -> None:
    verdict = _scored_review([_s("pass", 9), _s("pass", 8), _s("pass", 9), _s("pass", 8)])
    assert verdict.status is CapabilityStatus.ALLOWED
    assert len(verdict.lenses) == 4
    assert any(v.lens == "Security" for v in verdict.lenses)
    assert all(v.score is not None for v in verdict.lenses)


def test_scored_review_below_threshold_pends_even_when_all_pass() -> None:
    # Every lens says "pass" but the weighted total is < 7.0 -> not shippable.
    verdict = _scored_review([_s("pass", 5), _s("pass", 5), _s("pass", 6), _s("pass", 5)])
    assert verdict.status is CapabilityStatus.PENDING
    assert "ship threshold" in verdict.reason


def test_scored_review_security_floor_denies_despite_high_total() -> None:
    # High scores elsewhere, but Security reports score 1 -> hard fail.
    verdict = _scored_review([_s("pass", 10), _s("pass", 10), _s("pass", 1), _s("pass", 10)])
    assert verdict.status is CapabilityStatus.DENIED
    assert "security floor" in verdict.reason


def test_scored_review_security_block_is_a_floor() -> None:
    verdict = _scored_review(
        [_s("pass", 9), _s("pass", 9), _s("block", 4, "hardcoded secret"), _s("pass", 9)]
    )
    assert verdict.status is CapabilityStatus.DENIED
    assert "security floor" in verdict.reason


def test_scored_review_missing_score_escalates_not_passes() -> None:
    # A reply with no score is unusable -> ESCALATE (fail closed), never a silent ship.
    verdict = _scored_review(
        [json.dumps({"decision": "pass", "reason": "looks fine"}),
         _s("pass", 9), _s("pass", 9), _s("pass", 9)]
    )
    assert verdict.status is CapabilityStatus.PENDING
    assert any(v.decision is LensDecision.ESCALATE for v in verdict.lenses)


def test_scored_review_requires_structured_output() -> None:
    with pytest.raises(RuntimeError, match="structured output"):
        review_return_scored("g", (), "evidence", PlainBrain())


def test_ship_threshold_is_seven() -> None:
    assert SHIP_THRESHOLD == 7.0
