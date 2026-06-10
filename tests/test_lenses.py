from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.models import CapabilityRequest, CapabilityStatus, Task, TaskStatus


@dataclass
class _StubLensBrain:
    """A stand-in local model for the lenses: structured replies from a fixed
    per-lens script, recording every prompt it is asked to judge."""

    decisions: dict[str, str] = field(default_factory=dict)  # lens name -> decision
    reply_override: str | None = None  # force a raw (e.g. unparseable) reply
    calls: list[tuple[str, str]] = field(default_factory=list)  # (lens name, prompt)

    def think_json(self, agent_name, role, task, prompt, schema):
        self.calls.append((agent_name, prompt))
        if self.reply_override is not None:
            return self.reply_override
        decision = self.decisions.get(agent_name, "pass")
        return json.dumps({"decision": decision, "reason": f"{agent_name} stub {decision}"})


def _setup(tmp_path, brain):
    (tmp_path / ".orac").mkdir()
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    store = BrokerStore(tmp_path).init()
    broker = ToolBroker.from_store(store, repo_root=tmp_path, council_brain=brain)
    return broker, store


def _write(tmp_path, task, broker, content="X = 1\n"):
    return broker.request(
        CapabilityRequest(
            agent="Builder",
            tool="repo.write_file",
            task_id=task.id,
            args={"path": str(tmp_path / "x.py"), "content": content},
        ),
        task,
    )


def test_llm_lens_block_denies_the_write(tmp_path) -> None:
    brain = _StubLensBrain(decisions={"Efficiency": "block"})
    broker, store = _setup(tmp_path, brain)
    task = Task(title="write", status=TaskStatus.IN_PROGRESS)

    result = _write(tmp_path, task, broker)

    assert result.status is CapabilityStatus.DENIED
    assert "Efficiency" in result.message
    assert not (tmp_path / "x.py").exists()  # blocked before dispatch
    reviews = store.list_reviews(task.id)
    assert any(r["lens"] == "Efficiency" and r["decision"] == "block" for r in reviews)


def test_all_lenses_pass_allows_and_writes(tmp_path) -> None:
    brain = _StubLensBrain()  # every lens passes
    broker, _ = _setup(tmp_path, brain)
    task = Task(title="write", status=TaskStatus.IN_PROGRESS)

    result = _write(tmp_path, task, broker)

    assert result.status is CapabilityStatus.ALLOWED
    assert (tmp_path / "x.py").read_text() == "X = 1\n"
    # all three judgement lenses were consulted, each on its own call
    assert sorted(name for name, _ in brain.calls) == ["Efficiency", "Optimise", "Simple"]


def test_reads_are_not_sent_to_the_model(tmp_path) -> None:
    brain = _StubLensBrain(decisions={"Efficiency": "block"})  # would block if asked
    broker, _ = _setup(tmp_path, brain)
    task = Task(title="look", status=TaskStatus.IN_PROGRESS)

    result = broker.request(
        CapabilityRequest(agent="Builder", tool="git.status", task_id=task.id, args={"root": str(tmp_path)}),
        task,
    )

    assert result.status is CapabilityStatus.ALLOWED  # deterministic floor only
    assert brain.calls == []  # the model was never consulted for a read


def test_llm_lens_escalate_parks_the_task(tmp_path) -> None:
    brain = _StubLensBrain(decisions={"Optimise": "escalate"})
    broker, store = _setup(tmp_path, brain)
    task = Task(title="write", status=TaskStatus.IN_PROGRESS)

    result = _write(tmp_path, task, broker)

    assert result.status is CapabilityStatus.PENDING
    assert "Optimise" in result.message
    assert [p.tool for p in store.list_pending()] == ["repo.write_file"]
    assert not (tmp_path / "x.py").exists()


def test_unusable_reply_escalates_rather_than_passing(tmp_path) -> None:
    brain = _StubLensBrain(reply_override="I am not sure, let me think about it.")
    broker, store = _setup(tmp_path, brain)
    task = Task(title="write", status=TaskStatus.IN_PROGRESS)

    result = _write(tmp_path, task, broker)

    assert result.status is CapabilityStatus.PENDING  # conservative, not a silent pass
    reviews = store.list_reviews(task.id)
    assert any(r["decision"] == "escalate" and "usable verdict" in r["reason"] for r in reviews)


def test_eval_scorecard_scores_decisive_and_borderline() -> None:
    # Deterministic check of the eval harness's scoring (no model calls): a
    # must-pass that passed and a must-fail that escalated are correct; a
    # must-fail that passed is the rubber-stamp miss; borderline never counts
    # toward decisive accuracy.
    from orac.lens_eval import EvalCase, print_scorecard
    from orac.models import LensDecision, LensVerdict

    def case(kind):
        return EvalCase("Simple", kind, f"{kind} case", "goal", "x")

    def verdict(decision):
        return LensVerdict(lens="Simple", decision=decision, reason="r")

    perfect = [
        (case("pass"), verdict(LensDecision.PASS)),
        (case("fail"), verdict(LensDecision.ESCALATE)),
        (case("borderline"), verdict(LensDecision.PASS)),
    ]
    assert print_scorecard(perfect) == 0  # all decisive correct

    rubber_stamp = [
        (case("pass"), verdict(LensDecision.PASS)),
        (case("fail"), verdict(LensDecision.PASS)),  # missed a real violation
    ]
    assert print_scorecard(rubber_stamp) == 1


def test_each_lens_reviews_through_its_own_persona(tmp_path) -> None:
    brain = _StubLensBrain()
    broker, _ = _setup(tmp_path, brain)
    task = Task(
        title="write", status=TaskStatus.IN_PROGRESS, metadata={"goal": "ship a small change"}
    )

    _write(tmp_path, task, broker)

    prompts = {name: prompt for name, prompt in brain.calls}
    # Optimise judges through its own skill (prompts/optimiser.md mentions 60%)
    assert "60%" in prompts["Optimise"]
    # and every lens sees the concrete edge and the locked goal
    for prompt in prompts.values():
        assert "repo.write_file" in prompt
        assert "ship a small change" in prompt
