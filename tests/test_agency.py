from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from orac.agent_registry import load_agent_profiles
from orac.agent_session import AgentSession, parse_decision
from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.driver import originate
from orac.models import Board, Task, TaskStatus
from orac.scrum import Scrum
from orac.subtasks import run_goal_build


@dataclass
class ScriptedBrain:
    """A stand-in model: replies from a fixed script, records its prompts.

    The session runtime is identical in production; only the mind is canned.
    """

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


def _builder_profile():
    return next(p for p in load_agent_profiles() if p.slug == "builder")


def _builder_script(tmp_path) -> list[str]:
    mod = str(tmp_path / "mod.py")
    test = str(tmp_path / "test_mod.py")
    return [
        json.dumps({"tool": "git.create_branch", "args": {"root": str(tmp_path), "name": "build/x"}}),
        json.dumps({"tool": "repo.write_file", "args": {"path": mod, "content": "def add(a, b):\n    return a + b\n"}}),
        json.dumps({"tool": "repo.write_file", "args": {"path": test, "content": "from mod import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"}}),
        json.dumps({"tool": "git.commit", "args": {"root": str(tmp_path), "message": "add module", "paths": [mod, test]}}),
        json.dumps({"tool": "repo.run_tests", "args": {"root": str(tmp_path), "target": test}}),
        json.dumps({"done": True, "summary": "Added add() with a passing test on build/x."}),
    ]


def test_session_model_drives_real_work_through_the_broker(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    brain = ScriptedBrain(_builder_script(tmp_path))
    task = Task(title="build", status=TaskStatus.IN_PROGRESS)

    result = AgentSession(profile=_builder_profile(), brain=brain, broker=broker).run(
        task, contract="GOAL: add a tiny module with a passing test."
    )

    assert result.status == "done"
    assert (tmp_path / "mod.py").is_file()
    # every model choice went through the broker and is audited
    audited = [e.tool for e in store.audit_log()]
    assert "git.create_branch" in audited and "repo.run_tests" in audited
    # the transcript fed observations back to the model
    assert "OBSERVATION" in brain.prompts[-1]


def test_session_denial_is_an_observation_the_model_adapts_to(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    # Model first tries an ungranted tool (Builder has no status_reporter),
    # sees the denial, then finishes honestly.
    brain = ScriptedBrain(
        [
            json.dumps({"tool": "status_reporter", "args": {}}),
            json.dumps({"blocked": True, "reason": "status_reporter denied; nothing else needed."}),
        ]
    )
    task = Task(title="adapt", status=TaskStatus.IN_PROGRESS)

    result = AgentSession(profile=_builder_profile(), brain=brain, broker=broker).run(
        task, contract="GOAL: anything."
    )

    assert result.status == "blocked"
    assert "denied" in brain.prompts[-1]  # the refusal reached the model as an observation


def test_session_unparseable_reply_blocks_without_crashing(tmp_path) -> None:
    broker, _ = _setup(tmp_path)
    brain = ScriptedBrain(["I think I should probably create a branch first?"])
    task = Task(title="prose", status=TaskStatus.IN_PROGRESS)

    result = AgentSession(profile=_builder_profile(), brain=brain, broker=broker).run(
        task, contract="GOAL: anything."
    )

    assert result.status == "blocked"
    assert "Unparseable" in result.summary


def test_parse_decision_tolerates_fences_only() -> None:
    assert parse_decision('```json\n{"done": true, "summary": "x"}\n```') == {
        "done": True,
        "summary": "x",
    }
    assert parse_decision("let me explain my plan...") is None


def test_run_goal_build_rolls_summary_up(tmp_path) -> None:
    broker, _ = _setup(tmp_path)
    board = Board()
    parent = Task(title="improve", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    child = run_goal_build(
        board, parent,
        goal="add a tiny module",
        acceptance_criteria=("tests pass",),
        brain=ScriptedBrain(_builder_script(tmp_path)),
        broker=broker, repo_root=str(tmp_path),
    )

    assert child.status == TaskStatus.DONE
    assert "passing test" in parent.work_log[-1].message


def test_driver_originates_locked_ready_task_from_telemetry(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    board = Board()  # idle: empty
    goal_json = json.dumps(
        {
            "goal": "add a regression test for the broker grant check",
            "why": "audit shows no coverage of denial paths",
            "acceptance_criteria": ["new test exists", "suite passes"],
        }
    )
    brain = ScriptedBrain([goal_json])

    origination = originate(board, store, brain, tmp_path)

    assert origination is not None
    task = origination.task
    assert task.status == TaskStatus.READY  # intent pre-answered from the mandate and locked
    assert task.metadata["origin"] == "optimise-driver"
    assert task.metadata["build_goal"] == origination.goal
    assert task.acceptance_criteria == ["new test exists", "suite passes"]
    # telemetry reached the model
    assert "tasks_by_status" in brain.prompts[0]


def test_driver_respects_daily_cap_and_busy_board(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    goal = json.dumps({"goal": "g", "why": "w", "acceptance_criteria": ["c"]})

    busy = Board(tasks=[Task(title="active", status=TaskStatus.IN_PROGRESS)])
    assert originate(busy, store, ScriptedBrain([goal]), tmp_path) is None

    idle = Board()
    assert originate(idle, store, ScriptedBrain([goal]), tmp_path, daily_cap=1) is not None
    # idle again (originated task is READY = active, so clear it)
    idle.tasks[0].transition(TaskStatus.DONE)
    assert originate(idle, store, ScriptedBrain([goal]), tmp_path, daily_cap=1) is None


def test_scrum_loop_runs_origination_and_goal_build_end_to_end(tmp_path) -> None:
    # The full circle: idle board -> driver originates -> next cycle the
    # Builder session really builds it -> DONE with real files on disk.
    _setup(tmp_path)
    goal_json = json.dumps(
        {
            "goal": "add a tiny module",
            "why": "coverage gap",
            "acceptance_criteria": ["tests pass"],
        }
    )
    next_goal = json.dumps(
        {"goal": "another improvement", "why": "still idle", "acceptance_criteria": ["c"]}
    )
    # script: originate -> build it -> (loop is never idle: originates again)
    brain = ScriptedBrain([goal_json] + _builder_script(tmp_path) + [next_goal])
    scrum = Scrum(brain, root=tmp_path, originate_when_idle=True)
    board = Board()

    scrum.run(board, cycles=1)  # idle -> originates
    originated = [t for t in board.tasks if t.metadata.get("origin") == "optimise-driver"]
    assert len(originated) == 1 and originated[0].status == TaskStatus.READY

    scrum.run(board, cycles=1)  # builds it for real, then originates the next
    assert originated[0].status == TaskStatus.DONE
    assert (tmp_path / "mod.py").is_file()
    follow_on = [
        t for t in board.tasks
        if t.metadata.get("origin") == "optimise-driver" and t.status == TaskStatus.READY
    ]
    assert len(follow_on) == 1  # never idle: the next goal is already queued


def test_driver_fault_surfaces_as_blocked_task(tmp_path) -> None:
    _setup(tmp_path)
    scrum = Scrum(ScriptedBrain(["the answer is to improve things"]), root=tmp_path,
                  originate_when_idle=True)
    board = Board()

    scrum.run(board, cycles=1)

    faults = [t for t in board.tasks if t.metadata.get("origin") == "optimise-driver-fault"]
    assert len(faults) == 1
    assert faults[0].status == TaskStatus.BLOCKED
