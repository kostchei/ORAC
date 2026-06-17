from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.chat_config import save_chat_config
from orac.comms_adapters import CommsAdapterSet, comms_adapters_for
from orac.credentials import CredentialError, CredentialStore
from orac.models import (
    CapabilityRequest,
    CapabilityStatus,
    Externality,
    Reversibility,
    RiskClass,
    Task,
)
from orac.policy import ApprovalMode, approval_mode_for, risk_class
from orac.storage import BoardStore


class FakeBackend:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.history: list[dict[str, Any]] = []

    def read(self, target: str) -> list[dict[str, Any]]:
        return self.history

    def send(self, target: str, text: str) -> str:
        self.sent.append((target, text))
        return f"msg_{len(self.sent)}"


def _init_repo(path: Path) -> None:
    (path / ".orac").mkdir()
    (path / ".gitignore").write_text(".orac/\n", encoding="utf-8")


def _broker_with_fakes(tmp_path) -> tuple[ToolBroker, FakeBackend, FakeBackend]:
    _init_repo(tmp_path)
    store = BrokerStore(tmp_path).init()
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    slack, whatsapp = FakeBackend(), FakeBackend()
    broker.adapters.update(
        comms_adapters_for(tmp_path, slack_backend=slack, whatsapp_backend=whatsapp)
    )
    return broker, slack, whatsapp


def _req(tool: str, **args) -> CapabilityRequest:
    return CapabilityRequest(agent="Messenger", tool=tool, task_id="t1", args=args)


# --- risk model: the governance split -----------------------------------------


def test_read_and_draft_are_auto_send_is_approve() -> None:
    assert approval_mode_for("channel.read") is ApprovalMode.AUTO
    assert approval_mode_for("channel.draft") is ApprovalMode.AUTO
    # Sending is the irreversible-external action that must park for a human.
    assert risk_class("channel.send") == RiskClass(
        Reversibility.IRREVERSIBLE, Externality.EXTERNAL_PRIVATE
    )
    assert approval_mode_for("channel.send") is ApprovalMode.APPROVE


# --- fail-closed: no credentials, no backend ----------------------------------


def test_fail_closed_without_credentials(tmp_path) -> None:
    _init_repo(tmp_path)
    store = BrokerStore(tmp_path).init()
    broker = ToolBroker.from_store(store, repo_root=tmp_path)  # real (unconfigured) backends
    task = Task(title="t")
    with pytest.raises(CredentialError, match="Slack credentials"):
        broker.request(_req("channel.read", channel="slack", target="C1"), task)
    with pytest.raises(CredentialError, match="WhatsApp credentials"):
        broker.request(_req("channel.read", channel="whatsapp", target="+61400000000"), task)


# --- governed path: read/draft run, send parks --------------------------------


def test_read_runs_through_broker(tmp_path) -> None:
    broker, slack, _ = _broker_with_fakes(tmp_path)
    slack.history = [{"sender": "U1", "text": "hi"}]
    res = broker.request(_req("channel.read", channel="slack", target="C1"), Task(title="t"))
    assert res.status is CapabilityStatus.ALLOWED
    assert res.data["messages"][0]["text"] == "hi"


def test_send_parks_for_approval_and_does_not_dispatch(tmp_path) -> None:
    broker, slack, _ = _broker_with_fakes(tmp_path)
    res = broker.request(
        _req("channel.send", channel="slack", target="C1", text="hello"), Task(title="t")
    )
    assert res.status is CapabilityStatus.PENDING
    assert slack.sent == []  # nothing left the building before a human approved


def test_send_dispatches_once_approved(tmp_path) -> None:
    broker, slack, _ = _broker_with_fakes(tmp_path)
    task = Task(title="t")
    req = _req("channel.send", channel="slack", target="C1", text="hello")
    parked = broker.request(req, task)
    broker.store.resolve_pending(parked.data["pending_id"], "approved")
    res = broker.request(req, task)
    assert res.status is CapabilityStatus.ALLOWED
    assert slack.sent == [("C1", "hello")]
    assert res.data["message_id"] == "msg_1"
    assert "rollback_contract" not in res.data  # no fabricated inverse for an irreversible send


# --- draft path containment (the improvement over the salvaged version) -------


def test_draft_writes_contained_artifact(tmp_path) -> None:
    adapters = CommsAdapterSet(tmp_path)
    res = adapters.channel_draft(_req("channel.draft", channel="slack", target="C1", text="hi"))
    path = Path(res.data["path"])
    assert path.read_text(encoding="utf-8") == "hi"
    assert (tmp_path / ".orac" / "outputs" / "comms").resolve() in path.parents


def test_draft_target_cannot_escape_outputs_dir(tmp_path) -> None:
    adapters = CommsAdapterSet(tmp_path)
    res = adapters.channel_draft(
        _req("channel.draft", channel="slack", target="../../../evil", text="x")
    )
    path = Path(res.data["path"])
    assert (tmp_path / ".orac" / "outputs" / "comms").resolve() in path.parents
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path.parent / "evil").exists()


def test_unknown_channel_raises(tmp_path) -> None:
    adapters = CommsAdapterSet(tmp_path)
    with pytest.raises(ValueError, match="Unknown channel"):
        adapters.channel_draft(_req("channel.draft", channel="signal", target="x", text="y"))
