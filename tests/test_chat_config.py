from __future__ import annotations

import sys

import pytest

from orac.chat_config import (
    channel_enabled,
    is_authorized_sender,
    load_chat_config,
    save_chat_config,
)
from orac.cli import main
from orac.storage import BoardStore

win_only = pytest.mark.skipif(
    sys.platform != "win32", reason="connect-slack seals tokens via Windows DPAPI"
)


def test_defaults_are_fail_closed(tmp_path) -> None:
    cfg = load_chat_config(BoardStore(tmp_path))
    assert cfg["enabled"] is False
    assert channel_enabled(cfg, "slack") is False
    # nobody is authorised by default, even if a name is passed
    assert is_authorized_sender(cfg, "slack", "U123") is False


def test_authorization_requires_enabled_and_allowlisted(tmp_path) -> None:
    store = BoardStore(tmp_path)
    cfg = load_chat_config(store)
    cfg["enabled"] = True
    cfg["channels"]["slack"]["enabled"] = True
    cfg["channels"]["slack"]["authorized_senders"] = ["U123"]
    save_chat_config(store, cfg)

    cfg = load_chat_config(store)
    assert is_authorized_sender(cfg, "slack", "U123") is True
    assert is_authorized_sender(cfg, "slack", "U999") is False     # not allowlisted
    assert is_authorized_sender(cfg, "whatsapp", "U123") is False  # wrong channel


def test_channel_off_blocks_even_allowlisted_sender(tmp_path) -> None:
    store = BoardStore(tmp_path)
    cfg = load_chat_config(store)
    cfg["enabled"] = True
    cfg["channels"]["slack"]["enabled"] = False
    cfg["channels"]["slack"]["authorized_senders"] = ["U123"]
    save_chat_config(store, cfg)
    assert is_authorized_sender(load_chat_config(store), "slack", "U123") is False


def test_save_preserves_other_config_sections(tmp_path) -> None:
    store = BoardStore(tmp_path)
    store.save_json(store.config_path, {"model_policy": {"lmstudio_standard_model": "keep-me"}})
    cfg = load_chat_config(store)
    cfg["enabled"] = True
    save_chat_config(store, cfg)
    full = store.load_json(store.config_path, {})
    assert full["model_policy"]["lmstudio_standard_model"] == "keep-me"  # untouched
    assert full["chat"]["enabled"] is True


def test_cli_allow_and_status(tmp_path, capsys) -> None:
    BoardStore(tmp_path).init()
    assert main(["--root", str(tmp_path), "chat", "allow", "slack", "U42"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "chat", "status"]) == 0
    out = capsys.readouterr().out
    assert "slack" in out and "U42" in out


@win_only
def test_cli_connect_slack_seals_tokens_and_enables(tmp_path, capsys) -> None:
    from orac.credentials import CredentialStore

    BoardStore(tmp_path).init()
    rc = main([
        "--root", str(tmp_path), "chat", "connect-slack",
        "--bot-token", "xoxb-abc", "--app-token", "xapp-def",
    ])
    assert rc == 0

    store = BoardStore(tmp_path)
    cfg = load_chat_config(store)
    assert cfg["enabled"] is True and cfg["channels"]["slack"]["enabled"] is True
    creds = CredentialStore(tmp_path)
    assert creds.get("chat.slack.bot_token") == "xoxb-abc"
    assert creds.get("chat.slack.app_token") == "xapp-def"
    # the secret is sealed, not written to config.json
    assert "xoxb-abc" not in store.config_path.read_text(encoding="utf-8")


@win_only
def test_cli_disconnect_deletes_secrets(tmp_path) -> None:
    from orac.credentials import CredentialStore

    BoardStore(tmp_path).init()
    main([
        "--root", str(tmp_path), "chat", "connect-slack",
        "--bot-token", "xoxb-abc", "--app-token", "xapp-def",
    ])
    assert main(["--root", str(tmp_path), "chat", "disconnect", "slack"]) == 0
    creds = CredentialStore(tmp_path)
    assert creds.get("chat.slack.bot_token") is None
    assert load_chat_config(BoardStore(tmp_path))["channels"]["slack"]["enabled"] is False
