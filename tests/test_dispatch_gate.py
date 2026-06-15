from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.dispatch import ACTIVE_SLICE_CEILING, both_agree, optimise_admits
from orac.intent_ledger import is_covered, unsatisfied
from orac.models import Board, Task, TaskStatus
from orac.work import run_decomposed_goal, run_orchestrated_goal


def _store(tmp_path) -> BrokerStore:
    (tmp_path / ".orac").mkdir()
    return BrokerStore(tmp_path).init()


def _repo(tmp_path):
    (tmp_path / ".orac").mkdir()
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    store = BrokerStore(tmp_path).init()
    return ToolBroker.from_store(store, repo_root=tmp_path), store


# --- the gate, deterministically -------------------------------------------


def test_no_spawn_without_orchestrator_proposal(tmp_path) -> None:
    store = _store(tmp_path)
    decision = both_agree(store, orchestrator_proposed=False, resource_slice=0.25)
    assert not decision.agreed
    assert "did not propose" in decision.reason


def test_both_agree_when_slot_and_band_available(tmp_path) -> None:
    store = _store(tmp_path)
    decision = both_agree(store, orchestrator_proposed=True, resource_slice=0.25)
    assert decision.agreed


def test_optimise_refuses_when_band_full(tmp_path) -> None:
    store = _store(tmp_path)
    # fill the band exactly (4 x 0.25 = 1.0 = ceiling)
    for _ in range(4):
        store.admit_subagent("p", "builder", "i", "intent", 0.25)
    decision = optimise_admits(store, 0.25, band=ACTIVE_SLICE_CEILING)
    assert not decision.agreed
    assert "band full" in decision.reason


def test_optimise_refuses_when_roster_full(tmp_path) -> None:
    store = _store(tmp_path)
    store.admit_subagent("p", "builder", "i", "intent", 0.1, cap=1)
    decision = optimise_admits(store, 0.1, cap=1)
    assert not decision.agreed
    assert "roster full" in decision.reason


# --- band throttles a fan-out (slice deferred, parent stays open) -----------


def test_decomposed_goal_defers_slice_when_band_is_full(tmp_path) -> None:
    broker, store = _repo(tmp_path)
    # pre-occupy the whole band with active subagents from elsewhere
    for _ in range(4):
        store.admit_subagent("other", "builder", "i", "intent", 0.25)
    board = Board()
    parent = Task(title="needs room", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    # brain must never be consulted: the gate defers before any session runs
    @dataclass
    class NeverBrain:
        def think(self, *a, **k):  # noqa: ANN001
            raise AssertionError("no spawn should happen when the band is full")
        def think_json(self, *a, **k):  # noqa: ANN001
            raise AssertionError("no spawn should happen when the band is full")

    children = run_decomposed_goal(
        board, parent, "deliver it",
        [{"sub_intent": "only slice", "goal": "do it"}],
        "code", NeverBrain(), broker, {"repo_root": str(tmp_path)},
    )

    assert children == []                       # nothing spawned
    assert parent.status is TaskStatus.IN_PROGRESS  # stays open, not done/blocked
    assert len(unsatisfied(parent)) == 1        # the slice is still owed
    assert any("deferred" in e.message for e in parent.work_log)


def test_subagent_crash_retires_resource_reservation(tmp_path) -> None:
    broker, store = _repo(tmp_path)
    board = Board()
    parent = Task(title="crashy", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    @dataclass
    class CrashBrain:
        def think_json(self, *a, **k):  # noqa: ANN001
            raise RuntimeError("model transport died")

    children = run_decomposed_goal(
        board, parent, "deliver it",
        [{"sub_intent": "slice", "goal": "do it", "acceptance_criteria": ["ok"]}],
        "code", CrashBrain(), broker, {"repo_root": str(tmp_path)},
    )

    assert len(children) == 1
    assert children[0].status is TaskStatus.BLOCKED
    assert store.active_slice_total() == 0
    assert store.subagent_roster_count() == 0
    assert any("subagent crashed" in entry.message for entry in parent.work_log)


# --- the full orchestrated fan-out -----------------------------------------


@dataclass
class OrchestratedBrain:
    """One scripted think_json drives propose -> review -> each builder session."""

    script: list[str]
    prompts: list[str] = field(default_factory=list)

    def think_json(self, agent: str, role: str, task: Task, prompt: str, schema: dict) -> str:
        self.prompts.append(prompt)
        if not self.script:
            raise AssertionError("OrchestratedBrain ran out of script.")
        return self.script.pop(0)


def _plan(*subs: str) -> str:
    return json.dumps(
        {"slices": [{"sub_intent": s, "goal": f"add {s}", "acceptance_criteria": ["ok"]} for s in subs]}
    )


def _review_pass() -> list[str]:
    return [json.dumps({"decision": "pass", "reason": "fine"})] * 3


def _builder(tmp_path, suffix: str, branch: str) -> list[str]:
    mod = str(tmp_path / f"mod_{suffix}.py")
    test = str(tmp_path / f"test_{suffix}.py")
    return [
        json.dumps({"tool": "git.create_branch", "args": {"root": str(tmp_path), "name": branch}}),
        json.dumps({"tool": "repo.write_file", "args": {"path": mod, "content": "def v():\n    return 1\n"}}),
        json.dumps({"tool": "repo.write_file", "args": {"path": test, "content": f"from mod_{suffix} import v\n\ndef test_v():\n    assert v() == 1\n"}}),
        json.dumps({"tool": "git.commit", "args": {"root": str(tmp_path), "message": f"add {suffix}", "paths": [mod, test]}}),
        json.dumps({"done": True, "summary": f"built {suffix}"}),
    ]


def test_orchestrated_goal_full_flow(tmp_path) -> None:
    broker, store = _repo(tmp_path)
    board = Board()
    parent = Task(title="two parts", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)
    script = (
        [_plan("a", "b")]              # propose (frame)
        + _review_pass()              # plan review (counterweight)
        + _builder(tmp_path, "a", "build/a") + _review_pass()   # slice a + RETURN review
        + _builder(tmp_path, "b", "build/b") + _review_pass()   # slice b + RETURN review
    )

    children = run_orchestrated_goal(
        board, parent, "build two modules", "deliver both modules",
        "code", OrchestratedBrain(script), broker, {"repo_root": str(tmp_path)},
    )

    assert len(children) == 2
    assert parent.status is TaskStatus.DONE
    assert is_covered(parent)
    assert any("plan review passed" in e.message for e in parent.work_log)
    # The RETURN edge was reviewed before each slice integrated.
    assert any("done (verified + RETURN review)" in e.message for e in parent.work_log)


def test_subagent_recursion_fans_out_a_flagged_slice(tmp_path) -> None:
    # A slice flagged `decompose` is itself fanned out (a sub-parent with its own
    # plan/review/leaf) rather than run as a single doer — bounded recursion.
    broker, store = _repo(tmp_path)
    board = Board()
    parent = Task(title="big goal", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    top_plan = json.dumps({"slices": [
        {"sub_intent": "big", "goal": "add big",
         "acceptance_criteria": ["ok"], "decompose": True},
    ]})
    sub_plan = json.dumps({"slices": [
        {"sub_intent": "leaf", "goal": "add leaf", "acceptance_criteria": ["ok"]},
    ]})
    script = (
        [top_plan] + _review_pass()                                # top: propose + plan review
        + [sub_plan] + _review_pass()                              # recursive: propose + plan review
        + _builder(tmp_path, "leaf", "build/leaf") + _review_pass()  # leaf build + RETURN review
    )

    children = run_orchestrated_goal(
        board, parent, "build a big thing", "deliver the big thing",
        "code", OrchestratedBrain(script), broker, {"repo_root": str(tmp_path)},
    )

    assert len(children) == 1
    sub_parent = children[0]
    assert sub_parent.title.startswith("[decompose]")   # fanned out, not a single doer
    assert sub_parent.status is TaskStatus.DONE
    assert parent.status is TaskStatus.DONE
    assert is_covered(parent)
    leaves = [t for t in board.tasks if t.parent_id == sub_parent.id]
    assert leaves and any(t.status is TaskStatus.DONE for t in leaves)


def test_recursion_respects_max_depth(tmp_path) -> None:
    # At max depth a flagged slice runs as a single doer instead of nesting deeper.
    broker, store = _repo(tmp_path)
    board = Board()
    parent = Task(title="leaf only", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    decomposition = [
        {"sub_intent": "x", "goal": "add x", "acceptance_criteria": ["ok"], "decompose": True},
    ]
    script = _builder(tmp_path, "x", "build/x") + _review_pass()  # one doer + RETURN review only

    children = run_decomposed_goal(
        board, parent, "deliver x", decomposition, "code",
        OrchestratedBrain(script), broker, {"repo_root": str(tmp_path)},
        review_return=True, depth=2, max_depth=2,  # already at the depth bound
    )

    assert len(children) == 1
    # ran as a single doer slice, not a re-decomposed sub-parent
    assert not children[0].title.startswith("[decompose]")
    assert children[0].status is TaskStatus.DONE


def test_orchestrated_goal_rejected_plan_spawns_nothing(tmp_path) -> None:
    broker, store = _repo(tmp_path)
    board = Board()
    parent = Task(title="two parts", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)
    # propose, then Intent blocks the plan -> no builder steps should be consumed
    script = [_plan("a", "b"), json.dumps({"decision": "block", "reason": "misses the goal"}),
              json.dumps({"decision": "pass", "reason": "ok"}),
              json.dumps({"decision": "pass", "reason": "ok"})]

    children = run_orchestrated_goal(
        board, parent, "build two modules", "deliver both modules",
        "code", OrchestratedBrain(script), broker, {"repo_root": str(tmp_path)},
    )

    assert children == []
    assert parent.status is TaskStatus.BLOCKED
    assert store.subagent_roster_count() == 0  # nothing was admitted
    assert any("not accepted by plan review" in e.message for e in parent.work_log)
