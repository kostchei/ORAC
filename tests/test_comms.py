from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.chat_config import save_chat_config
from orac.comms_adapters import CommsBackend, comms_adapters_for
from orac.credentials import CredentialError, CredentialStore
from orac.models import CapabilityRequest, CapabilityStatus, Task
from orac.rollback_contract import RollbackContractError, apply_rollback, validate_contract
from orac.storage import BoardStore


class FakeCommsBackend:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.read_history: list[dict[str, Any]] = []

    def read(self, target: str) -> list[dict[str, Any]]:
        return self.read_history

    def send(self, target: str, text: str) -> str:
        self.sent.append((target, text))
        return f"msg_{len(self.sent)}"


def _init_repo(path: Path) -> None:
    (path / ".orac").mkdir()
    (path / ".gitignore").write_text(".orac/\n", encoding="utf-8")


def _store(path: Path) -> BrokerStore:
    return BrokerStore(path).init()


def test_fail_closed_raises_credential_error_when_no_creds(tmp_path) -> None:
    _init_repo(tmp_path)
    store = _store(tmp_path)
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(title="test comms fail-closed")

    # Slack: unconfigured ref chat.slack.bot_token
    with pytest.raises(CredentialError, match="Slack credentials are not configured"):
        broker.request(
            CapabilityRequest(
                agent="Messenger",
                tool="channel.read",
                task_id=task.id,
                args={"channel": "slack", "target": "C123"},
            ),
            task,
        )

    # WhatsApp: unconfigured ref chat.whatsapp.session
    with pytest.raises(CredentialError, match="WhatsApp credentials are not configured"):
        broker.request(
            CapabilityRequest(
                agent="Messenger",
                tool="channel.read",
                task_id=task.id,
                args={"channel": "whatsapp", "target": "+61400000000"},
            ),
            task,
        )


def test_channel_read_and_draft_are_auto(tmp_path) -> None:
    _init_repo(tmp_path)
    store = _store(tmp_path)
    
    # Enable Slack and WhatsApp in chat config, and set credential refs
    chat_cfg = {
        "enabled": True,
        "channels": {
            "slack": {
                "enabled": True,
                "authorized_senders": [],
                "bot_token_ref": "chat.slack.bot_token",
            },
            "whatsapp": {
                "enabled": True,
                "authorized_senders": [],
                "session_ref": "chat.whatsapp.session",
                "bridge_url": "http://localhost:8788",
            }
        }
    }
    board_store = BoardStore(tmp_path)
    board_store.init()
    save_chat_config(board_store, chat_cfg)

    # Set mock credentials in DPAPI vault
    creds = CredentialStore(tmp_path)
    creds.set("chat.slack.bot_token", "fake-slack-token")
    creds.set("chat.whatsapp.session", "fake-whatsapp-session")

    # Inject mock backends
    slack_fake = FakeCommsBackend()
    slack_fake.read_history = [{"sender": "U123", "text": "hey"}]
    whatsapp_fake = FakeCommsBackend()

    adapters = comms_adapters_for(
        tmp_path, slack_backend=slack_fake, whatsapp_backend=whatsapp_fake
    )
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    broker.adapters.update(adapters)
    
    task = Task(title="journaling")

    # channel.read must run immediately (status: ALLOWED)
    res_read = broker.request(
        CapabilityRequest(
            agent="Messenger",
            tool="channel.read",
            task_id=task.id,
            args={"channel": "slack", "target": "C123"},
        ),
        task,
    )
    assert res_read.status is CapabilityStatus.ALLOWED
    assert res_read.data["messages"] == [{"sender": "U123", "text": "hey"}]

    # channel.draft must run immediately (status: ALLOWED) and write draft file
    res_draft = broker.request(
        CapabilityRequest(
            agent="Messenger",
            tool="channel.draft",
            task_id=task.id,
            args={"channel": "slack", "target": "C123", "text": "proposed draft text"},
        ),
        task,
    )
    assert res_draft.status is CapabilityStatus.ALLOWED
    draft_file = tmp_path / ".orac" / "outputs" / "draft_slack_C123.txt"
    assert draft_file.exists()
    assert draft_file.read_text(encoding="utf-8") == "proposed draft text"


def test_channel_send_parks_and_dispatches_on_approval(tmp_path) -> None:
    _init_repo(tmp_path)
    store = _store(tmp_path)

    # Setup config + creds
    board_store = BoardStore(tmp_path)
    board_store.init()
    chat_cfg = {
        "enabled": True,
        "channels": {
            "slack": {
                "enabled": True,
                "authorized_senders": [],
                "bot_token_ref": "chat.slack.bot_token",
            }
        }
    }
    save_chat_config(board_store, chat_cfg)
    CredentialStore(tmp_path).set("chat.slack.bot_token", "fake-token")

    slack_fake = FakeCommsBackend()
    adapters = comms_adapters_for(tmp_path, slack_backend=slack_fake)
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    broker.adapters.update(adapters)

    task = Task(title="approve send")

    req = CapabilityRequest(
        agent="Messenger",
        tool="channel.send",
        task_id=task.id,
        args={"channel": "slack", "target": "C123", "text": "send this approved text"},
    )

    # 1. First request parks as PENDING
    res_park = broker.request(req, task)
    assert res_park.status is CapabilityStatus.PENDING
    pending_id = res_park.data["pending_id"]
    assert pending_id is not None
    assert slack_fake.sent == []

    # Verify no retrospective notification is queued yet
    assert len(store.list_notifications()) == 0

    # 2. Approve the parked request
    store.resolve_pending(pending_id, "approved")

    # 3. Request again, should run (status: ALLOWED)
    res_allow = broker.request(req, task)
    assert res_allow.status is CapabilityStatus.ALLOWED
    assert slack_fake.sent == [("C123", "send this approved text")]
    assert res_allow.data["message_id"] == "msg_1"

    # 4. Retrospective notification carries the rollback contract
    notifications = store.list_notifications()
    assert len(notifications) == 1
    note = notifications[0]
    assert note.tool == "channel.send"
    
    contract = note.data["rollback_contract"]
    validate_contract(contract)
    assert contract["inverse_operation"]["operation"] == "channel.post_correction"
    assert contract["inverse_operation"]["state_before"]["target"] == "C123"
    assert contract["inverse_operation"]["state_before"]["text"] == "send this approved text"

    # 5. Rollback contract raises RollbackContractError for human-in-the-loop correction
    with pytest.raises(RollbackContractError, match="cannot auto-undo; send a correction to C123, then ack."):
        apply_rollback(contract)
