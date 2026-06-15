"""Chat control-plane config + the sender allowlist (Phase 0).

The `chat` block in `.orac/config.json` describes the channels (Slack, WhatsApp),
whether each is enabled, who is allowed to drive ORAC through it, and the
`credential_ref`s where the channel's secrets live (the secrets themselves are in
the DPAPI credential store, never here). Authorisation is **fail-closed**: an
empty allowlist authorises nobody, so a channel can never be driven until an
operator explicitly adds their own sender id.
"""
from __future__ import annotations

import copy
from typing import Any

from orac.storage import BoardStore

CHANNELS = ("slack", "whatsapp")

DEFAULT_CHAT_CONFIG: dict[str, Any] = {
    # Master switch: the control plane does nothing until this is on.
    "enabled": False,
    # Inbound flood guard (per sender, per channel).
    "inbound_rate_per_min": 10,
    "channels": {
        "slack": {
            "enabled": False,
            # Slack user ids (e.g. "U0123ABCD") allowed to control ORAC. Empty = none.
            "authorized_senders": [],
            "bot_token_ref": "chat.slack.bot_token",
            "app_token_ref": "chat.slack.app_token",
        },
        "whatsapp": {
            "enabled": False,
            # Phone numbers (E.164, e.g. "+61400000000") allowed. Empty = none.
            "authorized_senders": [],
            "session_ref": "chat.whatsapp.session",
            # The local Node bridge (Baileys) ORAC's gateway talks to.
            "bridge_url": "http://localhost:8788",
        },
    },
}


def _merge(default: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    """Default ∪ saved, one level into ``channels`` so new default keys appear
    even on an old saved config (missing keys take the default)."""
    merged = copy.deepcopy(default)
    for key, value in saved.items():
        if key == "channels" and isinstance(value, dict):
            for channel, spec in value.items():
                base = merged["channels"].get(channel, {})
                base.update(spec if isinstance(spec, dict) else {})
                merged["channels"][channel] = base
        else:
            merged[key] = value
    return merged


def load_chat_config(store: BoardStore) -> dict[str, Any]:
    config = store.load_json(store.config_path, {})
    return _merge(DEFAULT_CHAT_CONFIG, config.get("chat", {}))


def save_chat_config(store: BoardStore, chat: dict[str, Any]) -> None:
    # Preserve the rest of config.json (e.g. model_policy) — only the chat block.
    config = store.load_json(store.config_path, {})
    config["chat"] = _merge(DEFAULT_CHAT_CONFIG, chat)
    store.save_json(store.config_path, config)


def _channel(cfg: dict[str, Any], channel: str) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ValueError(f"Unknown chat channel {channel!r}; expected one of {CHANNELS}.")
    return cfg.get("channels", {}).get(channel, {})


def channel_enabled(cfg: dict[str, Any], channel: str) -> bool:
    return bool(cfg.get("enabled")) and bool(_channel(cfg, channel).get("enabled"))


def is_authorized_sender(cfg: dict[str, Any], channel: str, sender: str) -> bool:
    """Fail-closed: only an enabled channel of an enabled control plane, with the
    sender explicitly on its allowlist, is authorised. Everything else is False."""
    if not channel_enabled(cfg, channel):
        return False
    return sender in set(_channel(cfg, channel).get("authorized_senders", []))
