from __future__ import annotations

import pytest

from orac.agents import build_core_agents
from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.intent_backbone import IntentBackbone, IntentField
from orac.llm import RulesBrain
from orac.models import Board, CapabilityRequest, CapabilityStatus, Task, TaskStatus
from orac.scrum import Scrum


def _make_store(tmp_path) -> BrokerStore:
    (tmp_path / ".orac").mkdir()
    return BrokerStore(tmp_path).init()


def test_store_seeds_manifest_grants(tmp_path) -> None:
    store = _make_store(tmp_path)

    grants = store.grants()

    assert "handoff_tracker" in grants["Optimiser"]
    assert "minimal_path_planner" in grants["Simples"]


def test_store_seed_is_idempotent(tmp_path) -> None:
    store = _make_store(tmp_path)
    before = store.grants()

    store.init()

    assert store.grants() == before


def test_grant_and_revoke(tmp_path) -> None:
    store = _make_store(tmp_path)

    store.grant("Simples", "status_reporter")
    assert "status_reporter" in store.grants()["Simples"]

    store.revoke("Simples", "status_reporter")
    assert "status_reporter" not in store.grants()["Simples"]


def test_broker_records_audit_for_every_decision(tmp_path) -> None:
    store = _make_store(tmp_path)
    broker = ToolBroker.from_store(store)
    task = Task(title="demo")

    broker.request(
        CapabilityRequest(
            agent="Simples",
            tool="minimal_path_planner",
            task_id=task.id,
            args={"candidate_steps": ["a", "b"]},
        ),
        task,
    )
    broker.request(
        CapabilityRequest(agent="Intent", tool="nope", task_id=task.id), task
    )

    log = store.audit_log()
    statuses = {entry.tool: entry.status for entry in log}
    assert statuses["minimal_path_planner"] == CapabilityStatus.ALLOWED.value
    assert statuses["nope"] == CapabilityStatus.ERROR.value


def test_pending_approval_lifecycle(tmp_path) -> None:
    store = _make_store(tmp_path)
    req = CapabilityRequest(agent="Simples", tool="implementation_log", task_id="t1")

    pending_id = store.create_pending(req)
    assert [p.id for p in store.list_pending()] == [pending_id]

    store.resolve_pending(pending_id, "approved")
    assert store.list_pending() == []


def test_resolve_unknown_pending_raises(tmp_path) -> None:
    store = _make_store(tmp_path)

    with pytest.raises(KeyError):
        store.resolve_pending(999, "approved")


def test_store_backed_scrum_loop_writes_audit_trail(tmp_path) -> None:
    (tmp_path / ".orac").mkdir()
    task = Task(title="Build the thing", description="Make it testable.")
    intent = IntentBackbone()
    for field in IntentField:
        intent.answer(task, field, f"{field.value} answer")
    intent.lock(task)
    board = Board(tasks=[task])

    Scrum(RulesBrain(), root=tmp_path).run(board, cycles=3)

    assert board.tasks[0].status == TaskStatus.DONE
    log = BrokerStore(tmp_path).audit_log()
    assert log, "expected the routed tool calls to be audited"
    assert all(entry.status == CapabilityStatus.ALLOWED.value for entry in log)
    audited_tools = {entry.tool for entry in log}
    assert {"minimal_path_planner", "verification_log", "handoff_tracker"} <= audited_tools


def test_fs_read_runs_without_approval(tmp_path) -> None:
    # fs_read is local + reversible -> auto under the risk model: no pending row.
    store = _make_store(tmp_path)
    store.grant("Simples", "fs_read")
    broker = ToolBroker.from_store(store)
    target = tmp_path / "note.txt"
    target.write_text("hello orac", encoding="utf-8")
    task = Task(title="read a file")
    req = CapabilityRequest(
        agent="Simples", tool="fs_read", task_id=task.id, args={"path": str(target)}
    )

    result = broker.request(req, task)

    assert result.status is CapabilityStatus.ALLOWED
    assert result.data["content"] == "hello orac"
    assert store.list_pending() == []


def test_fs_read_missing_file_raises(tmp_path) -> None:
    store = _make_store(tmp_path)
    store.grant("Simples", "fs_read")
    broker = ToolBroker.from_store(store)
    task = Task(title="read a file")
    req = CapabilityRequest(
        agent="Simples",
        tool="fs_read",
        task_id=task.id,
        args={"path": str(tmp_path / "does-not-exist.txt")},
    )

    with pytest.raises(FileNotFoundError):
        broker.request(req, task)


def test_loop_parks_and_resumes_task_on_approval(tmp_path) -> None:
    store = _make_store(tmp_path)
    task = Task(title="Build the thing", description="Make it testable.")
    intent = IntentBackbone()
    for field in IntentField:
        intent.answer(task, field, f"{field.value} answer")
    intent.lock(task)  # task now READY

    # Park the task on an as-yet-unresolved approval, as the loop would.
    req = CapabilityRequest(agent="Simples", tool="fs_read", task_id=task.id)
    pending_id = store.create_pending(req)
    task.park_for_approval(pending_id, TaskStatus.READY)
    board = Board(tasks=[task])

    # While pending, the loop leaves the task parked.
    Scrum(RulesBrain(), root=tmp_path).run(board, cycles=1)
    assert board.tasks[0].status == TaskStatus.PENDING_APPROVAL

    # After approval, the loop resumes it from READY and drives it to DONE.
    store.resolve_pending(pending_id, "approved")
    Scrum(RulesBrain(), root=tmp_path).run(board, cycles=3)
    assert board.tasks[0].status == TaskStatus.DONE


def test_agent_work_parks_task_on_pending(tmp_path, monkeypatch) -> None:
    store = _make_store(tmp_path)
    store.grant("Builder", "git.push")
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    agent = next(a for a in build_core_agents(RulesBrain(), broker) if a.name == "Builder")
    task = Task(title="push", status=TaskStatus.IN_PROGRESS)

    # Pin the risk verdict to APPROVE: this test exercises the park machinery,
    # independent of which tools currently classify as approval-gated (code work
    # is review-after; APPROVE is reserved for comms/financial/physical).
    from orac.policy import ApprovalMode

    monkeypatch.setattr(
        "orac.broker.approval_mode_for", lambda tool, args=None: ApprovalMode.APPROVE
    )

    agent._apply_builtin_action = lambda t: bool(
        agent._use(t, "git.push", root=str(tmp_path))
    )
    agent.work(task)

    assert task.status == TaskStatus.PENDING_APPROVAL
    assert task.metadata["pending_approval"]["resume_status"] == "in_progress"


def test_rate_counter_increments(tmp_path) -> None:
    store = _make_store(tmp_path)

    assert store.bump_rate("Simples", "implementation_log", "2026-06-09") == 1
    assert store.bump_rate("Simples", "implementation_log", "2026-06-09") == 2
    assert store.rate_count("Simples", "implementation_log", "2026-06-09") == 2
    assert store.rate_count("Simples", "implementation_log", "2026-06-10") == 0
