from __future__ import annotations

from pathlib import Path
import pytest

from orac.agent_registry import load_agent_profiles
from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.events_adapters import events_adapters_for
from orac.events_store import EventsStore
from orac.models import CapabilityRequest, CapabilityStatus, Task, TaskStatus
from orac.policy import risk_class
from orac.work import WORK_KINDS, verify_goal_done


def test_host_agent_registered() -> None:
    profiles = {p.slug: p for p in load_agent_profiles()}
    assert "host" in profiles
    host = profiles["host"]
    assert host.name == "Host"
    assert host.kind == "doer"
    assert "event.create" in host.tools
    assert "event.ask_human" in host.tools
    assert "event.close" in host.tools


def test_events_store_lifecycle(tmp_path: Path) -> None:
    store = EventsStore(tmp_path)

    # 1. Create event
    ev = store.create_event(title="Strategy Workshop", total_rounds=3, task_id="t101", metadata={"facilitator": "Alice"})
    assert ev.title == "Strategy Workshop"
    assert ev.total_rounds == 3
    assert ev.current_round == 1
    assert ev.status == "active"
    assert ev.state["facilitator"] == "Alice"

    # 2. Add participants
    p1 = store.add_participant(ev.id, name="Alice", role="judge", channel="local")
    p2 = store.add_participant(ev.id, name="Bob", role="participant", channel="slack")
    participants = store.list_participants(ev.id)
    assert len(participants) == 2
    assert participants[0].name == "Alice"
    assert participants[1].name == "Bob"

    # 3. Ask human input
    h_req = store.ask_human(ev.id, question="Approve theme A or B?", options=["A", "B"], participant_id=p1.id)
    assert h_req.status == "pending"
    assert store.get_event(ev.id).status == "waiting_human"

    pending_list = store.list_pending_human_requests(ev.id)
    assert len(pending_list) == 1
    assert pending_list[0].id == h_req.id

    # 4. Respond to human input
    answered = store.respond_human(h_req.id, response="Theme A approved")
    assert answered.status == "answered"
    assert answered.response == "Theme A approved"
    assert store.get_event(ev.id).status == "active"

    # 5. Broadcast message
    b = store.broadcast_message(ev.id, message="Starting round 1 discussion.", channel="all")
    assert b.message == "Starting round 1 discussion."
    assert len(store.list_broadcasts(ev.id)) == 1

    # 6. Advance round
    ev = store.advance_round(ev.id, summary="Round 1 concluded with consensus on theme A.")
    assert ev.current_round == 2
    assert ev.status == "active"

    # 7. Advance through remaining rounds and close
    ev = store.advance_round(ev.id, summary="Round 2 concluded.")
    assert ev.current_round == 3
    assert ev.status == "active"

    ev = store.close_event(ev.id, summary="Workshop finished successfully.")
    assert ev.status == "completed"
    assert ev.state["closing_summary"] == "Workshop finished successfully."


def test_events_adapters_through_broker(tmp_path: Path) -> None:
    bstore = BrokerStore(tmp_path).init()
    broker = ToolBroker.from_store(bstore, repo_root=tmp_path)
    task = Task(id="task_ev_1", title="Host meeting", work_kind="event")

    # event.create
    req_create = CapabilityRequest(
        agent="Host",
        tool="event.create",
        task_id=task.id,
        args={"title": "Team Retro", "total_rounds": 2},
    )
    res_create = broker.request(req_create, task)
    assert res_create.status == CapabilityStatus.ALLOWED
    event_id = res_create.data["event"]["id"]

    # event.add_participant
    req_part = CapabilityRequest(
        agent="Host",
        tool="event.add_participant",
        task_id=task.id,
        args={"event_id": event_id, "name": "Charlie", "role": "participant"},
    )
    res_part = broker.request(req_part, task)
    assert res_part.status == CapabilityStatus.ALLOWED

    # event.ask_human
    req_ask = CapabilityRequest(
        agent="Host",
        tool="event.ask_human",
        task_id=task.id,
        args={"event_id": event_id, "question": "What went well?"},
    )
    res_ask = broker.request(req_ask, task)
    assert res_ask.status == CapabilityStatus.ALLOWED
    req_id = res_ask.data["request"]["id"]

    # event.wait_for_response (pending)
    req_wait = CapabilityRequest(
        agent="Host",
        tool="event.wait_for_response",
        task_id=task.id,
        args={"request_id": req_id},
    )
    res_wait = broker.request(req_wait, task)
    assert res_wait.status == CapabilityStatus.ALLOWED
    assert res_wait.data["request"]["status"] == "pending"

    # Answer request via store directly (simulating user answering)
    estore = EventsStore(tmp_path)
    estore.respond_human(req_id, "Fast iterations")

    # event.wait_for_response (answered)
    res_wait2 = broker.request(req_wait, task)
    assert res_wait2.status == CapabilityStatus.ALLOWED
    assert res_wait2.data["request"]["status"] == "answered"
    assert res_wait2.data["request"]["response"] == "Fast iterations"

    # event.advance_round
    req_adv = CapabilityRequest(
        agent="Host",
        tool="event.advance_round",
        task_id=task.id,
        args={"event_id": event_id, "summary": "Round 1 done"},
    )
    res_adv = broker.request(req_adv, task)
    assert res_adv.status == CapabilityStatus.ALLOWED

    # event.close
    req_close = CapabilityRequest(
        agent="Host",
        tool="event.close",
        task_id=task.id,
        args={"event_id": event_id, "summary": "Retro completed"},
    )
    res_close = broker.request(req_close, task)
    assert res_close.status == CapabilityStatus.ALLOWED
    assert res_close.data["event"]["status"] == "completed"


def test_verify_event_closed(tmp_path: Path) -> None:
    bstore = BrokerStore(tmp_path).init()
    broker = ToolBroker.from_store(bstore, repo_root=tmp_path)
    estore = EventsStore(tmp_path)
    spec = WORK_KINDS["event"]

    child = Task(id="c_event_1", title="Host Checklist", work_kind="event")

    # Case 1: No event created -> verification fails
    ok, detail = verify_goal_done(spec, child, broker, {"repo_root": str(tmp_path)})
    assert not ok
    assert "no event session found" in detail

    # Case 2: Event created but still active -> verification fails
    ev = estore.create_event("Checklist Session", total_rounds=1, task_id=child.id)
    ok, detail = verify_goal_done(spec, child, broker, {"repo_root": str(tmp_path)})
    assert not ok
    assert "not completed" in detail

    # Case 3: Event closed -> verification passes
    estore.close_event(ev.id, summary="Checklist items verified.")
    ok, detail = verify_goal_done(spec, child, broker, {"repo_root": str(tmp_path)})
    assert ok
    assert "reached completed status" in detail
