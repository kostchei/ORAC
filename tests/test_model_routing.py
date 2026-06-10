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


def test_cli_models_set_persists_slot_and_preserves_others(tmp_path) -> None:
    import argparse

    from orac.cli import cmd_models_set

    store = BoardStore(tmp_path)
    store.init()
    ModelPolicyStore(store).save_policy({"lmstudio_code_model": "qwen3-coder-next"})

    rc = cmd_models_set(store, argparse.Namespace(slot="small", model="llama-3.2-3b"))

    assert rc == 0
    policy = ModelPolicyStore(store).load_policy()
    assert policy["lmstudio_small_model"] == "llama-3.2-3b"  # the lens model is set
    assert policy["lmstudio_code_model"] == "qwen3-coder-next"  # other slots untouched


def test_lens_brain_uses_small_slot_with_structured_output(tmp_path) -> None:
    from orac.model_policy import lens_brain

    store = BoardStore(tmp_path)
    store.init()
    policy_store = ModelPolicyStore(store)
    policy_store.save_policy({"lmstudio_small_model": "llama-3.2-3b"})

    brain = lens_brain(policy_store)

    assert isinstance(brain, LMStudioBrain)  # raw, no RulesBrain fallback
    assert brain.model == "llama-3.2-3b"
    assert callable(getattr(brain, "think_json", None))  # lenses need structured output


def test_verify_model_slots_passes_when_configured_models_are_loadable(tmp_path, monkeypatch) -> None:
    from orac.model_policy import verify_model_slots

    store = BoardStore(tmp_path)
    store.init()
    policy_store = ModelPolicyStore(store)
    policy_store.save_policy(
        {"lmstudio_standard_model": "mistral-small-3.1-24b-instruct-2503",
         "lmstudio_small_model": "gemma-4-12b"}  # bare; served id is google/gemma-4-12b
    )
    monkeypatch.setattr(
        "orac.model_policy.lmstudio_models",
        lambda base_url="x": ["mistral-small-3.1-24b-instruct-2503", "google/gemma-4-12b"],
    )
    monkeypatch.setattr("orac.model_policy.lmstudio_available_model_records", lambda: [])

    report = verify_model_slots(policy_store)

    assert report["checked"] is True
    assert report["ok"] is True
    assert report["missing"] == {}


def test_verify_model_slots_flags_a_stale_slot(tmp_path, monkeypatch) -> None:
    from orac.model_policy import verify_model_slots

    store = BoardStore(tmp_path)
    store.init()
    policy_store = ModelPolicyStore(store)
    policy_store.save_policy({"lmstudio_code_model": "qwen3.6-35b-a3b"})  # not loadable
    monkeypatch.setattr(
        "orac.model_policy.lmstudio_models", lambda base_url="x": ["google/gemma-4-12b"]
    )
    monkeypatch.setattr("orac.model_policy.lmstudio_available_model_records", lambda: [])

    report = verify_model_slots(policy_store)

    assert report["checked"] is True
    assert report["ok"] is False
    assert report["missing"] == {"lmstudio_code_model": "qwen3.6-35b-a3b"}


def test_verify_model_slots_skips_when_lmstudio_unreachable(tmp_path, monkeypatch) -> None:
    from orac.model_policy import verify_model_slots

    store = BoardStore(tmp_path)
    store.init()
    policy_store = ModelPolicyStore(store)
    policy_store.save_policy({"lmstudio_standard_model": "anything"})
    monkeypatch.setattr("orac.model_policy.lmstudio_models", lambda base_url="x": [])
    monkeypatch.setattr("orac.model_policy.lmstudio_available_model_records", lambda: [])

    report = verify_model_slots(policy_store)

    assert report["checked"] is False  # no info; not a failure
    assert report["ok"] is True


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


def test_local_retries_once_before_browser_escalation(tmp_path, monkeypatch) -> None:
    """First local failure → retry locally.  Second failure → escalate to browser."""
    _setup(tmp_path)
    monkeypatch.setenv("ORAC_BROWSER_FOUNDATION", "claude")
    monkeypatch.delenv("ORAC_FOUNDATION_API_KEY", raising=False)
    task = Task(
        title="too hard for local",
        status=TaskStatus.READY,
        work_kind="code",
        metadata={"goal": "do something local cannot"},
    )
    board = Board(tasks=[task])
    # LMStudio unreachable → FallbackBrain → RulesBrain prose → unparseable → blocked.
    scrum = Scrum(brain=None, root=tmp_path, route_models=True)

    # First failure: should retry locally, NOT escalate yet.
    scrum.run(board, cycles=1)
    assert task.metadata.get("local_failures") == 1
    assert task.metadata.get("escalated") is None
    assert task.status == TaskStatus.READY

    # Second failure: should now escalate to browser with a round-robin provider.
    scrum.run(board, cycles=1)
    assert task.metadata.get("local_failures") == 2
    assert task.metadata.get("escalated") is True
    assert task.metadata.get("browser_provider") in ("claude", "gemini", "openai")
    assert task.status == TaskStatus.READY

    # Third failure (browser also blocked) → stays BLOCKED for human.
    import orac.model_policy as model_policy

    monkeypatch.setattr(
        model_policy,
        "session_brain_for",
        lambda policy_store, t: StructuredScriptedBrain(
            [json.dumps({"blocked": True, "reason": "browser also failed"})]
        ),
    )
    scrum.run(board, cycles=1)
    assert task.status == TaskStatus.BLOCKED


def test_browser_provider_round_robin(tmp_path, monkeypatch) -> None:
    """next_browser_provider rotates claude → gemini → openai → claude."""
    from orac.model_policy import ModelPolicyStore, next_browser_provider, _BROWSER_PROVIDERS
    from orac.storage import BoardStore

    store = BoardStore(tmp_path)
    store.init()
    ps = ModelPolicyStore(store)

    providers = [next_browser_provider(ps) for _ in range(6)]
    assert providers == _BROWSER_PROVIDERS * 2


def test_browser_provider_stored_in_task_metadata(tmp_path, monkeypatch) -> None:
    """The assigned provider is stored in task.metadata so session_brain_for uses it."""
    from orac.model_policy import ModelPolicyStore, session_brain_for
    from orac.browser_brain import BrowserFoundationBrain
    from orac.llm import FallbackBrain
    from orac.storage import BoardStore

    monkeypatch.setenv("ORAC_BROWSER_FOUNDATION", "claude")
    monkeypatch.delenv("ORAC_FOUNDATION_API_KEY", raising=False)
    store = BoardStore(tmp_path)
    store.init()
    ps = ModelPolicyStore(store)

    task = Task(title="t", work_kind="code", metadata={"escalated": True, "browser_provider": "gemini"})
    brain = session_brain_for(ps, task)
    assert isinstance(brain, FallbackBrain)
    assert isinstance(brain.primary, BrowserFoundationBrain)
    # Uses the task-assigned provider, not the env-var default.
    assert brain.primary.provider == "gemini"
