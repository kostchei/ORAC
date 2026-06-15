from __future__ import annotations

import sys

import pytest

import orac.chat_signon as signon
from orac.chat_config import load_chat_config
from orac.chat_signon import (
    allow_sender,
    chat_status,
    connect_slack,
    disconnect_channel,
    prepare_whatsapp,
)
from orac.credentials import CredentialStore
from orac.storage import BoardStore

win_only = pytest.mark.skipif(
    sys.platform != "win32", reason="sign-on stores secrets via Windows DPAPI"
)


def test_chat_status_is_fail_closed_and_refs_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        signon,
        "whatsapp_bridge_status",
        lambda url: {"url": url, "reachable": False, "connected": False, "qr": None},
    )
    store = BoardStore(tmp_path)
    store.init()

    status = chat_status(store)

    assert status["enabled"] is False
    assert status["channels"]["slack"]["enabled"] is False
    assert status["channels"]["slack"]["credentials"]["bot_token_ref"] == {
        "ref": "chat.slack.bot_token",
        "stored": False,
    }
    assert "xoxb" not in str(status)


def test_allow_and_disallow_sender_persist(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        signon,
        "whatsapp_bridge_status",
        lambda url: {"url": url, "reachable": False, "connected": False, "qr": None},
    )
    store = BoardStore(tmp_path)
    store.init()

    allow_sender(store, "slack", "U42")
    assert load_chat_config(store)["channels"]["slack"]["authorized_senders"] == ["U42"]

    disallowed = signon.disallow_sender(store, "slack", "U42")
    assert disallowed["channels"]["slack"]["authorized_senders"] == []


@win_only
def test_connect_slack_seals_tokens_and_status_never_returns_them(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        signon,
        "whatsapp_bridge_status",
        lambda url: {"url": url, "reachable": False, "connected": False, "qr": None},
    )
    store = BoardStore(tmp_path)
    store.init()

    status = connect_slack(store, "xoxb-secret", "xapp-secret")

    assert status["enabled"] is True
    assert status["channels"]["slack"]["enabled"] is True
    assert status["channels"]["slack"]["credentials"]["bot_token_ref"]["stored"] is True
    assert "xoxb-secret" not in str(status)
    assert "xapp-secret" not in str(status)
    assert CredentialStore(tmp_path).get("chat.slack.bot_token") == "xoxb-secret"


@win_only
def test_prepare_whatsapp_can_store_session_and_enable_channel(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        signon,
        "whatsapp_bridge_status",
        lambda url: {"url": url, "reachable": True, "connected": True, "qr": None},
    )
    store = BoardStore(tmp_path)
    store.init()

    status = prepare_whatsapp(store, "http://localhost:9999", session="wa-session")

    assert status["enabled"] is True
    assert status["channels"]["whatsapp"]["enabled"] is True
    assert status["channels"]["whatsapp"]["bridge_url"] == "http://localhost:9999"
    assert "wa-session" not in str(status)
    assert CredentialStore(tmp_path).get("chat.whatsapp.session") == "wa-session"


@win_only
def test_disconnect_deletes_channel_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        signon,
        "whatsapp_bridge_status",
        lambda url: {"url": url, "reachable": False, "connected": False, "qr": None},
    )
    store = BoardStore(tmp_path)
    store.init()
    connect_slack(store, "xoxb-secret", "xapp-secret")

    status = disconnect_channel(store, "slack")

    assert status["channels"]["slack"]["enabled"] is False
    assert status["channels"]["slack"]["credentials"]["bot_token_ref"]["stored"] is False
    assert CredentialStore(tmp_path).get("chat.slack.bot_token") is None
