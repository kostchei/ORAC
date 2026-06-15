from __future__ import annotations

from orac.broker_store import BrokerStore
from orac.chat_config import load_chat_config, save_chat_config
from orac.chat_gateway import ChatGateway, InboundMessage, _chat_model_candidates
from orac.models import CapabilityRequest, CapabilityResult, CapabilityStatus, Task, TaskStatus
from orac.storage import BoardStore


def _enable(store: BoardStore, channel: str = "slack", sender: str = "U42") -> None:
    store.init()
    cfg = load_chat_config(store)
    cfg["enabled"] = True
    cfg["channels"][channel]["enabled"] = True
    cfg["channels"][channel]["authorized_senders"] = [sender]
    save_chat_config(store, cfg)


def _msg(text: str, sender: str = "U42") -> InboundMessage:
    return InboundMessage(channel="slack", sender=sender, text=text, reply_to="D42")


def test_unauthorized_sender_is_ignored(tmp_path) -> None:
    store = BoardStore(tmp_path)
    _enable(store)

    replies = ChatGateway(tmp_path).handle(_msg("status", sender="U999"))

    assert replies == []


def test_goal_command_creates_ready_code_goal(tmp_path) -> None:
    store = BoardStore(tmp_path)
    _enable(store)

    reply = ChatGateway(tmp_path).handle(_msg("goal: build the connector"))[0]

    assert "Added goal" in reply.text
    task = store.load().tasks[0]
    assert task.status == TaskStatus.READY
    assert task.work_kind == "code"
    assert task.metadata["goal"] == "build the connector"
    assert task.metadata["source_channel"] == "slack"


def test_status_and_reviews_report_queue(tmp_path) -> None:
    store = BoardStore(tmp_path)
    _enable(store)
    bstore = BrokerStore(tmp_path).init()
    bstore.create_pending(CapabilityRequest(agent="Builder", tool="git.push", task_id="t1"))

    gateway = ChatGateway(tmp_path)
    status = gateway.handle(_msg("status"))[0].text
    reviews = gateway.handle(_msg("reviews"))[0].text

    assert "1 pending approval" in status
    assert "pending [1]" in reviews


def test_approve_deny_and_ack_route_to_broker_store(tmp_path) -> None:
    store = BoardStore(tmp_path)
    _enable(store)
    bstore = BrokerStore(tmp_path).init()
    pending_id = bstore.create_pending(
        CapabilityRequest(agent="Builder", tool="git.push", task_id="t1")
    )
    note_id = bstore.record_notification(
        CapabilityRequest(agent="Builder", tool="git.push", task_id="t1"),
        CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool="git.push",
            message="pushed",
            data={},
        ),
    )

    gateway = ChatGateway(tmp_path)
    assert f"Approved [{pending_id}]" in gateway.handle(_msg(f"approve {pending_id}"))[0].text
    assert bstore.get_pending(pending_id).status == "approved"
    assert f"Acked [{note_id}]" in gateway.handle(_msg(f"ack {note_id}"))[0].text
    assert bstore.get_notification(note_id).acked is True


def test_freeform_question_does_not_dump_help(tmp_path, monkeypatch) -> None:
    store = BoardStore(tmp_path)
    _enable(store)
    monkeypatch.setattr(ChatGateway, "_naturalize", lambda self, raw, factual, **kw: factual)

    reply = ChatGateway(tmp_path).handle(_msg("what now"))[0].text

    assert "ORAC chat commands" not in reply
    assert "ORAC status:" in reply


def test_freeform_blocked_question_lists_blocked_tasks(tmp_path, monkeypatch) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    board.add_task(Task(title="Fix WhatsApp pairing", status=TaskStatus.BLOCKED))
    store.save(board)
    _enable(store)
    monkeypatch.setattr(ChatGateway, "_naturalize", lambda self, raw, factual, **kw: factual)

    reply = ChatGateway(tmp_path).handle(_msg("are the blocked tasks still relevant?"))[0].text

    assert "Blocked tasks:" in reply
    assert "Fix WhatsApp pairing" in reply
    assert "ORAC chat commands" not in reply


def test_chat_model_candidates_prefer_reasoning_then_small() -> None:
    policy = {
        "lmstudio_code_model": "qwen3.6-35b-a3b",
        "lmstudio_small_model": "google/gemma-4-12b",
        "lmstudio_standard_model": "mistral-small-3.1-24b-instruct-2503",
    }

    assert _chat_model_candidates(policy)[:3] == [
        "qwen3.6-35b-a3b",
        "google/gemma-4-12b",
        "mistral-small-3.1-24b-instruct-2503",
    ]


def test_gateway_writes_comms_message_log(tmp_path) -> None:
    store = BoardStore(tmp_path)
    _enable(store)

    ChatGateway(tmp_path).handle(_msg("status"))

    log_path = tmp_path / ".orac" / "comms_logs" / "messages.jsonl"
    assert log_path.exists()
    raw = log_path.read_text(encoding="utf-8")
    assert '"kind": "inbound"' in raw
    assert '"kind": "outbound"' in raw
    assert '"channel": "slack"' in raw


def test_poll_outbound_sends_on_review_queue_change(tmp_path) -> None:
    store = BoardStore(tmp_path)
    _enable(store)
    gateway = ChatGateway(tmp_path)
    assert gateway.poll_outbound() == []

    BrokerStore(tmp_path).init().create_pending(
        CapabilityRequest(agent="Builder", tool="git.push", task_id="t1")
    )
    out = gateway.poll_outbound()

    assert len(out) == 1
    assert out[0].channel == "slack"
    assert out[0].target == "U42"
    assert "pending [1]" in out[0].text
    assert gateway.poll_outbound() == []
