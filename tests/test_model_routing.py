from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from orac.agent_session import DECISION_SCHEMA, AgentSession
from orac.agent_registry import load_agent_profiles
from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.llm import FoundationBrain, LMStudioBrain
from orac.model_policy import (
    DEFAULT_POLICY,
    ModelPolicyStore,
    can_escalate,
    model_for_work_kind,
    session_brain_for,
)
from orac.models import Board, Task, TaskStatus
from orac.scrum import Scrum
from orac.storage import BoardStore


@dataclass
class StructuredScriptedBrain:
    """Scripted brain that supports structured output and records the schema."""

    script: list[str]
    schemas: list[dict] = field(default_factory=list)

    def think(self, agent_name: str, role: str, task: Task, prompt: str) -> str:
        raise AssertionError("think() must not be used when think_json exists")

    def think_json(
        self, agent_name: str, role: str, task: Task, prompt: str, schema: dict
    ) -> str:
        self.schemas.append(schema)
        return self.script.pop(0)


def _setup(tmp_path):
    (tmp_path / ".orac").mkdir()
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    return ToolBroker.from_store(BrokerStore(tmp_path).init(), repo_root=tmp_path)


def test_session_uses_structured_output_when_brain_supports_it(tmp_path) -> None:
    broker = _setup(tmp_path)
    brain = StructuredScriptedBrain([json.dumps({"done": True, "summary": "nothing to do"})])
    profile = next(p for p in load_agent_profiles() if p.slug == "builder")
    task = Task(title="t", status=TaskStatus.IN_PROGRESS)

    result = AgentSession(profile=profile, brain=brain, broker=broker).run(task, "GOAL: noop")

    assert result.status == "done"
    assert brain.schemas == [DECISION_SCHEMA]  # schema enforced server-side


def test_model_for_work_kind_uses_slots() -> None:
    policy = dict(DEFAULT_POLICY)
    policy.update(
        {
            "lmstudio_standard_model": "gpt-oss-20b",
            "lmstudio_code_model": "qwen3-coder-next",
            "lmstudio_creative_model": "mistral-small-3.1",
        }
    )

    assert model_for_work_kind(policy, "code") == "qwen3-coder-next"
    assert model_for_work_kind(policy, "media") == "mistral-small-3.1"
    assert model_for_work_kind(policy, "event") == "mistral-small-3.1"
    assert model_for_work_kind(policy, "comms") == "gpt-oss-20b"
    assert model_for_work_kind(policy, None) == "gpt-oss-20b"
    # empty slot falls through to standard
    policy["lmstudio_code_model"] = ""
    assert model_for_work_kind(policy, "code") == "gpt-oss-20b"


def test_can_escalate_requires_key_and_budget(tmp_path, monkeypatch) -> None:
    store = BoardStore(tmp_path)
    store.init()
    policy_store = ModelPolicyStore(store)

    monkeypatch.delenv("ORAC_FOUNDATION_API_KEY", raising=False)
    assert can_escalate(policy_store) is False

    monkeypatch.setenv("ORAC_FOUNDATION_API_KEY", "test-key")
    assert can_escalate(policy_store) is True

    policy_store.record_foundation_spend(10.0)  # blow the daily cap
    assert can_escalate(policy_store) is False


def test_session_brain_for_routes_by_kind_and_escalation(tmp_path) -> None:
    store = BoardStore(tmp_path)
    store.init()
    policy_store = ModelPolicyStore(store)
    policy_store.save_policy({"lmstudio_code_model": "qwen3-coder-next"})

    local = session_brain_for(policy_store, Task(title="t", work_kind="code"))
    assert isinstance(local.primary, LMStudioBrain)
    assert local.primary.model == "qwen3-coder-next"

    escalated = session_brain_for(
        policy_store, Task(title="t", work_kind="code", metadata={"escalated": True})
    )
    assert isinstance(escalated.primary, FoundationBrain)


def test_blocked_local_session_escalates_once_then_stays_blocked(tmp_path, monkeypatch) -> None:
    _setup(tmp_path)
    monkeypatch.setenv("ORAC_FOUNDATION_API_KEY", "test-key")
    task = Task(
        title="too hard for local",
        status=TaskStatus.READY,
        work_kind="code",
        metadata={"goal": "do something local cannot"},
    )
    board = Board(tasks=[task])
    # route_models=True: the session brain is LMStudio (connection refused in
    # tests) -> FallbackBrain -> RulesBrain prose -> unparseable -> blocked.
    scrum = Scrum(brain=None, root=tmp_path, route_models=True)

    scrum.run(board, cycles=1)

    assert task.metadata.get("escalated") is True
    assert task.status == TaskStatus.READY  # requeued for the foundation model
    assert any("escalated" in log.message for log in task.work_log)

    # Second failure (already escalated) stays BLOCKED for the human.
    import orac.model_policy as model_policy

    monkeypatch.setattr(
        model_policy,
        "session_brain_for",
        lambda policy_store, t: StructuredScriptedBrain(
            [json.dumps({"blocked": True, "reason": "still cannot"})]
        ),
    )
    scrum.run(board, cycles=1)

    assert task.status == TaskStatus.BLOCKED
