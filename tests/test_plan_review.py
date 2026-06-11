from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from orac.models import CapabilityStatus, Task
from orac.plan_review import review_decomposition


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
