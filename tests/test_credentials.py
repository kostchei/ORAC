from __future__ import annotations

import sys

import pytest

from orac.credentials import CredentialStore

win_only = pytest.mark.skipif(
    sys.platform != "win32", reason="credential store uses Windows DPAPI"
)


@win_only
def test_seal_and_open_roundtrip(tmp_path) -> None:
    store = CredentialStore(tmp_path)
    store.set("chat.slack.bot_token", "xoxb-secret-123")
    assert store.get("chat.slack.bot_token") == "xoxb-secret-123"
    assert store.has("chat.slack.bot_token")


@win_only
def test_secret_is_not_stored_in_plaintext(tmp_path) -> None:
    store = CredentialStore(tmp_path)
    store.set("chat.whatsapp.session", "super-secret-session")
    raw = store.path.read_text(encoding="utf-8")
    assert "super-secret-session" not in raw  # sealed, not plaintext


@win_only
def test_refs_lists_keys_not_secrets(tmp_path) -> None:
    store = CredentialStore(tmp_path)
    store.set("a", "secret-a")
    store.set("b", "secret-b")
    assert store.refs() == ["a", "b"]  # refs only


@win_only
def test_redact_scrubs_stored_secret_values(tmp_path) -> None:
    store = CredentialStore(tmp_path)
    store.set("chat.slack.bot_token", "xoxb-leak-me")
    line = "calling slack with token xoxb-leak-me for chat.slack.bot_token"
    redacted = store.redact(line)
    assert "xoxb-leak-me" not in redacted
    assert "***" in redacted
    assert "chat.slack.bot_token" in redacted  # the ref is safe to keep


@win_only
def test_delete_and_missing_ref(tmp_path) -> None:
    store = CredentialStore(tmp_path)
    assert store.get("nope") is None
    store.set("k", "v")
    assert store.delete("k") is True
    assert store.delete("k") is False
    assert store.get("k") is None


def test_credential_store_path_under_orac(tmp_path) -> None:
    # Platform-agnostic: the store lives in .orac/credentials.json.
    store = CredentialStore(tmp_path)
    assert store.path == tmp_path / ".orac" / "credentials.json"
