from __future__ import annotations

import pytest

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


def test_rate_counter_increments(tmp_path) -> None:
    store = _make_store(tmp_path)

    assert store.bump_rate("Simples", "implementation_log", "2026-06-09") == 1
    assert store.bump_rate("Simples", "implementation_log", "2026-06-09") == 2
    assert store.rate_count("Simples", "implementation_log", "2026-06-09") == 2
    assert store.rate_count("Simples", "implementation_log", "2026-06-10") == 0
