from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from orac.broker_store import MAX_SUBAGENTS, BrokerStore
from orac.models import Task
from orac.orchestrator import abundance_frame, propose_decomposition


@dataclass
class ScriptedBrain:
    script: list[str]
    prompts: list[str] = field(default_factory=list)

    def think(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.script:
            raise AssertionError("ScriptedBrain ran out of script.")
        return self.script.pop(0)


def _store(tmp_path) -> BrokerStore:
    (tmp_path / ".orac").mkdir()
    return BrokerStore(tmp_path).init()


def _plan(*subs: str) -> str:
    return json.dumps(
        {"slices": [{"sub_intent": s, "goal": f"do {s}", "acceptance_criteria": ["ok"]} for s in subs]}
    )


def test_frame_states_the_numbers_and_biases_to_decompose() -> None:
    frame = abundance_frame(487, 500)
    assert "487 of 500" in frame
    assert "DECOMPOSE" in frame


def test_propose_returns_validated_slices(tmp_path) -> None:
    store = _store(tmp_path)
    brain = ScriptedBrain([_plan("part a", "part b")])

    slices = propose_decomposition("build the feature", "the whole feature", store, brain)

    assert [s["sub_intent"] for s in slices] == ["part a", "part b"]
    assert slices[0]["goal"] == "do part a"


def test_frame_uses_the_live_free_count(tmp_path) -> None:
    store = _store(tmp_path)
    # fill some of the roster so the free count is below the cap
    for _ in range(3):
        store.admit_subagent("p", "builder", "instr", "intent", 0.1)
    brain = ScriptedBrain([_plan("a")])

    propose_decomposition("g", "i", store, brain)

    expected_free = MAX_SUBAGENTS - 3
    assert f"{expected_free} of {MAX_SUBAGENTS}" in brain.prompts[0]


def test_frame_self_tightens_to_zero_when_roster_full(tmp_path) -> None:
    store = _store(tmp_path)
    store.admit_subagent("p", "builder", "instr", "intent", 0.1, cap=1)
    brain = ScriptedBrain([_plan("a")])

    # roster full -> 0 free -> even a single-slice plan exceeds the budget
    with pytest.raises(ValueError, match="exceeds its honest budget"):
        propose_decomposition("g", "i", store, brain, cap=1)
    assert "0 of 1" in brain.prompts[0]


def test_plan_exceeding_free_budget_is_refused(tmp_path) -> None:
    store = _store(tmp_path)
    brain = ScriptedBrain([_plan("a", "b", "c")])

    with pytest.raises(ValueError, match="exceeds its honest budget"):
        propose_decomposition("g", "i", store, brain, cap=2)


def test_unparseable_plan_raises(tmp_path) -> None:
    store = _store(tmp_path)
    brain = ScriptedBrain(["let me think about how to break this down..."])

    with pytest.raises(ValueError, match="no parseable decomposition"):
        propose_decomposition("g", "i", store, brain)


def test_empty_slices_raises(tmp_path) -> None:
    store = _store(tmp_path)
    brain = ScriptedBrain([json.dumps({"slices": []})])

    with pytest.raises(ValueError, match="no slices"):
        propose_decomposition("g", "i", store, brain)
