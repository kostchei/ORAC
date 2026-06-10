from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.models import Board, Task, TaskStatus
from orac.work import WORK_KINDS, WorkKindSpec, run_goal_task, verify_goal_done


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


def _passing_build_script(tmp_path) -> list[str]:
    mod = str(tmp_path / "mod.py")
    test = str(tmp_path / "test_mod.py")
    return [
        json.dumps({"tool": "git.create_branch", "args": {"root": str(tmp_path), "name": "build/x"}}),
        json.dumps({"tool": "repo.write_file", "args": {"path": mod, "content": "def add(a, b):\n    return a + b\n"}}),
        json.dumps({"tool": "repo.write_file", "args": {"path": test, "content": "from mod import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"}}),
        json.dumps({"tool": "git.commit", "args": {"root": str(tmp_path), "message": "add module", "paths": [mod, test]}}),
        json.dumps({"tool": "repo.run_tests", "args": {"root": str(tmp_path), "target": test}}),
        json.dumps({"done": True, "summary": "Added add() with a passing test."}),
    ]


def _lying_build_script(tmp_path) -> list[str]:
    """A session that writes a FAILING test, never fixes it, then claims done.

    This is the live-fire failure mode the verifier exists to catch: a model
    declaring victory while the suite is red.
    """
    mod = str(tmp_path / "mod.py")
    test = str(tmp_path / "test_mod.py")
    return [
        json.dumps({"tool": "git.create_branch", "args": {"root": str(tmp_path), "name": "build/x"}}),
        json.dumps({"tool": "repo.write_file", "args": {"path": mod, "content": "def add(a, b):\n    return a - b\n"}}),
        json.dumps({"tool": "repo.write_file", "args": {"path": test, "content": "from mod import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"}}),
        json.dumps({"tool": "git.commit", "args": {"root": str(tmp_path), "message": "add module", "paths": [mod, test]}}),
        json.dumps({"done": True, "summary": "All done, looks great!"}),
    ]


def test_verified_done_passes_when_suite_is_green(tmp_path) -> None:
    broker, _ = _setup(tmp_path)
    board = Board()
    parent = Task(title="improve", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    child = run_goal_task(
        board, parent,
        goal="add a tiny module",
        acceptance_criteria=("tests pass",),
        work_kind="code",
        brain=ScriptedBrain(_passing_build_script(tmp_path)),
        broker=broker, context={"repo_root": str(tmp_path)},
    )

    assert child.status == TaskStatus.DONE
    assert "verified" in parent.work_log[-1].message


def test_self_reported_done_is_rejected_when_suite_is_red(tmp_path) -> None:
    broker, _ = _setup(tmp_path)
    board = Board()
    parent = Task(title="improve", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    child = run_goal_task(
        board, parent,
        goal="add a tiny module",
        acceptance_criteria=("tests pass",),
        work_kind="code",
        brain=ScriptedBrain(_lying_build_script(tmp_path)),
        broker=broker, context={"repo_root": str(tmp_path)},
    )

    # The model said done; the independent re-run found red tests and refused it.
    assert child.status == TaskStatus.BLOCKED
    assert parent.status == TaskStatus.BLOCKED
    assert "verification failed" in parent.work_log[-1].message.lower()
    assert any("claimed done" in entry.message.lower() for entry in child.work_log)


def test_verify_goal_done_without_repo_root_fails_closed(tmp_path) -> None:
    broker, _ = _setup(tmp_path)
    child = Task(title="[code] x", status=TaskStatus.IN_PROGRESS)

    ok, detail = verify_goal_done(WORK_KINDS["code"], child, broker, context={})

    assert ok is False
    assert "repo_root" in detail


def test_doer_kind_must_declare_a_verifier(tmp_path) -> None:
    broker, _ = _setup(tmp_path)
    board = Board()
    parent = Task(title="p", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    # Monkeypatch a doer-bearing kind with no verifier: spawning it must raise,
    # not silently trust the doer's self-reported done.
    original = WORK_KINDS["code"]
    WORK_KINDS["code"] = WorkKindSpec(
        kind="code", doer_slug="builder",
        done_means=original.done_means, contract_rules=original.contract_rules,
        verifier=None,
    )
    try:
        try:
            run_goal_task(
                board, parent, goal="x", acceptance_criteria=(),
                work_kind="code", brain=ScriptedBrain([]), broker=broker,
                context={"repo_root": str(tmp_path)},
            )
            raised = False
        except ValueError as exc:
            raised = "verifier" in str(exc)
    finally:
        WORK_KINDS["code"] = original

    assert raised


def test_every_doer_bearing_kind_has_a_verifier() -> None:
    # The invariant as a standing assertion: any kind with a doer must name a
    # verifier (the doer claims done; something else must confirm it).
    for spec in WORK_KINDS.values():
        if spec.doer_slug is not None:
            assert spec.verifier is not None, f"{spec.kind} has a doer but no verifier"
