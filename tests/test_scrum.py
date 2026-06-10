from __future__ import annotations

from orac.audio_io import audio_status
from orac.agent_registry import get_agent_protocol, get_tool_map, load_agent_profiles
from orac.intent_backbone import SPEC, IntentBackbone, IntentField
from orac.llm import RulesBrain
from orac.model_policy import ModelPolicyStore, ensure_lmstudio_model_loaded, select_lmstudio_model_for_ram
from orac.models import Board, Task, TaskStatus
from orac.resources import ResourceSnapshot
from orac.scrum import Scrum
from orac.storage import BoardStore
from orac.task_registry import TaskRegistry
from orac.tooling import RegularToolExecutor


def test_orac_agents_are_loaded_from_prompt_registry() -> None:
    profiles = load_agent_profiles()

    council = [profile.slug for profile in profiles if profile.kind == "council"]
    assert council == ["intent", "optimiser", "simples", "efficiency", "orchestrator"]
    assert "builder" in [profile.slug for profile in profiles]
    assert next(p for p in profiles if p.slug == "builder").kind == "doer"
    assert all(profile.system_prompt for profile in profiles)
    assert all(profile.protocol_file.endswith(".json") for profile in profiles)
    assert set(profiles[0].tools).issubset(get_tool_map())


def test_agent_protocol_specs_are_structured() -> None:
    expected_titles = {
        "intent": "Intent Translator MAX",
        "optimiser": "Optimiser Resource MAX",
        "simples": "Simples Minimal Path MAX",
        "efficiency": "Efficiency Waste Scan MAX",
        "orchestrator": "Orchestrator Reporter MAX",
    }
    for slug, title in expected_titles.items():
        spec = get_agent_protocol(slug)
        assert spec.title == title
        assert len(spec.steps) == 7
        assert spec.response_prompt

    assert SPEC.title == "Intent Translator MAX"
    assert SPEC.steps[0].name == "SILENT SCAN"
    assert SPEC.steps[3].name == "📝 BLUEPRINT"
    assert SPEC.steps[-1].name == "RESET"
    assert SPEC.response_prompt == "Ready—what do you need?"


def test_regular_tool_executor_updates_tasks() -> None:
    task = Task(title="Clarify the goal")
    tools = RegularToolExecutor()

    tools.run(
        "acceptance_criteria_editor",
        task,
        "Intent",
        criteria=["Goal is clear.", "Done state is checkable."],
    )

    assert task.acceptance_criteria == ["Goal is clear.", "Done state is checkable."]
    assert task.work_log[-1].agent == "Intent"


def test_task_registry_adds_base_request() -> None:
    board = Board()
    task = TaskRegistry(board).add_base_request("New request", "Do the thing.", points=2)

    assert task.metadata["request_type"] == "base_request"
    assert task.work_log[-1].kind == "user"
    assert TaskRegistry(board).stats().backlog == 1


def test_model_policy_uses_daily_foundation_cap(tmp_path) -> None:
    store = BoardStore(tmp_path)
    store.init()
    policy_store = ModelPolicyStore(store)
    policy_store.record_foundation_spend(0.1)
    decision = policy_store.decide()

    assert decision.daily_foundation_cap_usd == 0.45
    assert decision.foundation_spent_today_usd == 0.1
    assert decision.foundation_remaining_today_usd == 0.35


def test_lmstudio_selection_prefers_largest_tool_model_within_ram(monkeypatch) -> None:
    monkeypatch.setattr(
        "orac.model_policy.read_resource_snapshot",
        lambda target: ResourceSnapshot(
            cpu_percent=10,
            memory_percent=25,
            memory_total_gb=64,
            memory_available_gb=20,
            gpu_percent=None,
            vram_percent=None,
            disk_free_gb=100,
            busy=False,
            recommended_tier="local",
            reason="resources within policy",
        ),
    )
    monkeypatch.setattr(
        "orac.model_policy.lmstudio_available_model_records",
        lambda: [
            {"displayName": "Small Tool", "modelKey": "small", "sizeBytes": 6 * 1024**3, "trainedForToolUse": True},
            {"displayName": "Large Plain", "modelKey": "plain", "sizeBytes": 12 * 1024**3, "trainedForToolUse": False},
            {"displayName": "Large Tool", "modelKey": "tool", "sizeBytes": 11 * 1024**3, "trainedForToolUse": True},
            {"displayName": "Too Big", "modelKey": "too-big", "sizeBytes": 14 * 1024**3, "trainedForToolUse": True},
        ],
    )

    selected = select_lmstudio_model_for_ram({"target_local_resource_percent": 60})

    assert selected
    assert selected["modelKey"] == "tool"


def test_lmstudio_autoload_keeps_existing_loaded_model(monkeypatch) -> None:
    loaded = [{"identifier": "current-local", "modelKey": "existing"}]
    monkeypatch.setattr("orac.model_policy.lmstudio_start", lambda port: (True, "started"))
    monkeypatch.setattr("orac.model_policy.lmstudio_loaded_models", lambda: loaded)
    monkeypatch.setattr(
        "orac.model_policy.lmstudio_load_model",
        lambda model_key, identifier: (_ for _ in ()).throw(AssertionError("should not load a replacement")),
    )

    result = ensure_lmstudio_model_loaded({"lmstudio_url": "http://localhost:1234/v1"})

    assert result["ok"] is True
    assert result["action"] == "kept_loaded"
    assert result["loaded_models"] == loaded


def test_audio_status_is_serializable() -> None:
    status = audio_status().to_dict()

    assert "microphones" in status
    assert "speakers" in status
    assert "default_microphone" in status
    assert "default_speaker" in status
    assert "whisper_available" in status
    assert "tts_available" in status


def test_unlocked_task_stops_in_clarification() -> None:
    board = Board(tasks=[Task(title="Build the thing")])
    scrum = Scrum(RulesBrain())

    result = scrum.run(board, cycles=3)

    assert result.touched_tasks == 1
    assert board.tasks[0].status == TaskStatus.CLARIFYING
    assert result.done_tasks == 0
    assert "Next question" in board.tasks[0].work_log[0].message


def test_locked_intent_is_released_to_ready_by_the_gate() -> None:
    # The gate is the sole intent-axis mover: a locked task is released to
    # READY with goal + work_kind + acceptance_criteria fixed, ready for its
    # doer session. (The build itself needs a real model, exercised elsewhere.)
    task = Task(title="Build the thing", description="Make it testable.")
    intent = IntentBackbone()
    for field in IntentField:
        intent.answer(task, field, f"{field.value} answer")
    intent.lock(task)
    board = Board(tasks=[task])
    scrum = Scrum(RulesBrain())

    result = scrum.run(board, cycles=1)

    assert result.touched_tasks == 1
    assert board.tasks[0].status == TaskStatus.READY
    assert board.tasks[0].work_kind == "code"
    assert board.tasks[0].metadata["goal"]
    assert board.tasks[0].acceptance_criteria
    assert any(entry.agent == "Intent" for entry in board.tasks[0].work_log)


def test_sprint_plan_respects_capacity() -> None:
    board = Board(
        tasks=[
            Task(title="Small", points=2),
            Task(title="Large", points=8),
            Task(title="Medium", points=3),
        ]
    )
    scrum = Scrum(RulesBrain())

    planned = scrum.plan_sprint(board, capacity=5)

    assert [task.title for task in planned] == ["Small", "Medium"]
    assert all(task.status == TaskStatus.BACKLOG for task in planned)
