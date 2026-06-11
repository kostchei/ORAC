from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

import pytest

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.intent_ledger import (
    SLICE_BLOCKED,
    SLICE_SATISFIED,
    attach_child,
    coverage_report,
    is_blocked,
    is_covered,
    mark,
    open_ledger,
    slices,
    unsatisfied,
)
from orac.models import Board, Task, TaskStatus
from orac.work import run_decomposed_goal, settle_parent_against_ledger


# --- ledger unit behaviour (no model needed) -------------------------------


def _parent_with_ledger() -> Task:
    parent = Task(title="big goal", status=TaskStatus.IN_PROGRESS)
    open_ledger(
        parent,
        "deliver the whole feature",
        [
            {"sub_intent": "part A", "goal": "do A", "acceptance_criteria": ["a"]},
            {"sub_intent": "part B", "goal": "do B"},
        ],
    )
    return parent


def test_open_ledger_records_open_slices() -> None:
    parent = _parent_with_ledger()
    entries = slices(parent)
    assert [s["status"] for s in entries] == ["open", "open"]
    assert entries[1]["goal"] == "do B"  # goal defaults to sub_intent if absent
    assert not is_covered(parent)


def test_open_ledger_twice_is_refused() -> None:
    parent = _parent_with_ledger()
    with pytest.raises(ValueError, match="already has an intent ledger"):
        open_ledger(parent, "x", [{"sub_intent": "y"}])


def test_open_ledger_needs_slices() -> None:
    parent = Task(title="t")
    with pytest.raises(ValueError, match="no slices"):
        open_ledger(parent, "x", [])


def test_mark_and_coverage() -> None:
    parent = _parent_with_ledger()
    attach_child(parent, 0, "childA")
    attach_child(parent, 1, "childB")

    mark(parent, "childA", SLICE_SATISFIED)
    assert not is_covered(parent)
    assert len(unsatisfied(parent)) == 1
    assert "part B" in coverage_report(parent)

    mark(parent, "childB", SLICE_SATISFIED)
    assert is_covered(parent)
    assert "fully covered" in coverage_report(parent)


def test_mark_unknown_child_raises() -> None:
    parent = _parent_with_ledger()
    with pytest.raises(KeyError):
        mark(parent, "ghost", SLICE_SATISFIED)


def test_settle_all_open_keeps_parent_in_progress_and_reminds() -> None:
    parent = _parent_with_ledger()  # nothing marked yet
    settle_parent_against_ledger(parent)
    assert parent.status is TaskStatus.IN_PROGRESS
    assert "not finished" in parent.work_log[-1].message.lower()


def test_settle_all_satisfied_closes_parent() -> None:
    parent = _parent_with_ledger()
    attach_child(parent, 0, "a")
    attach_child(parent, 1, "b")
    mark(parent, "a", SLICE_SATISFIED)
    mark(parent, "b", SLICE_SATISFIED)

    settle_parent_against_ledger(parent)

    assert parent.status is TaskStatus.DONE


def test_settle_blocked_slice_blocks_parent() -> None:
    parent = _parent_with_ledger()
    attach_child(parent, 0, "a")
    attach_child(parent, 1, "b")
    mark(parent, "a", SLICE_SATISFIED)
    mark(parent, "b", SLICE_BLOCKED)

    settle_parent_against_ledger(parent)

    assert parent.status is TaskStatus.BLOCKED
    assert is_blocked(parent)


# --- run_decomposed_goal end to end ----------------------------------------


@dataclass
class ScriptedBrain:
    script: list[str]
    prompts: list[str] = field(default_factory=list)

    def think(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.script:
            raise AssertionError("ScriptedBrain ran out of script.")
        return self.script.pop(0)


def _setup(tmp_path):
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


def _slice_script(tmp_path, suffix: str, branch: str, passing: bool = True) -> list[str]:
    mod = str(tmp_path / f"mod_{suffix}.py")
    test = str(tmp_path / f"test_{suffix}.py")
    body = "return 1" if passing else "return 0"
    return [
        json.dumps({"tool": "git.create_branch", "args": {"root": str(tmp_path), "name": branch}}),
        json.dumps({"tool": "repo.write_file", "args": {"path": mod, "content": f"def v():\n    {body}\n"}}),
        json.dumps({"tool": "repo.write_file", "args": {"path": test, "content": f"from mod_{suffix} import v\n\ndef test_v():\n    assert v() == 1\n"}}),
        json.dumps({"tool": "git.commit", "args": {"root": str(tmp_path), "message": f"add {suffix}", "paths": [mod, test]}}),
        json.dumps({"done": True, "summary": f"built {suffix}"}),
    ]


def test_decomposed_goal_covers_intent_and_closes_parent(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    board = Board()
    parent = Task(title="two-part feature", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)
    decomposition = [
        {"sub_intent": "module a", "goal": "add a", "acceptance_criteria": ["a"]},
        {"sub_intent": "module b", "goal": "add b", "acceptance_criteria": ["b"]},
    ]
    brain = ScriptedBrain(
        _slice_script(tmp_path, "a", "build/a") + _slice_script(tmp_path, "b", "build/b")
    )

    children = run_decomposed_goal(
        board, parent, "deliver both modules", decomposition,
        "code", brain, broker, {"repo_root": str(tmp_path)},
    )

    assert len(children) == 2
    assert all(c.status is TaskStatus.DONE for c in children)
    assert parent.status is TaskStatus.DONE
    assert is_covered(parent)
    # both subagents were registered and retired done; the roster is clear again
    assert store.subagent_roster_count() == 0
    assert len(store.list_subagents(status="done")) == 2


def test_decomposed_goal_one_blocked_slice_blocks_parent(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    board = Board()
    parent = Task(title="two-part feature", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)
    decomposition = [
        {"sub_intent": "module a", "goal": "add a", "acceptance_criteria": ["a"]},
        {"sub_intent": "module b", "goal": "add b", "acceptance_criteria": ["b"]},
    ]
    # slice b writes a failing test -> verify_goal_done blocks it
    brain = ScriptedBrain(
        _slice_script(tmp_path, "a", "build/a")
        + _slice_script(tmp_path, "b", "build/b", passing=False)
    )

    run_decomposed_goal(
        board, parent, "deliver both modules", decomposition,
        "code", brain, broker, {"repo_root": str(tmp_path)},
    )

    assert parent.status is TaskStatus.BLOCKED
    assert not is_covered(parent)
    assert "module b" in coverage_report(parent)
    # the blocked slice's subagent is recorded blocked, not left dangling active
    assert len(store.list_subagents(status="blocked")) == 1
    assert len(store.list_subagents(status="done")) == 1
