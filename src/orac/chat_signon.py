"""Local chat sign-on helpers for Slack and WhatsApp.

The UI uses this module as the narrow write path into the credential store.
Payloads returned to the browser include credential refs and stored/missing
booleans only, never secret values.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from orac.chat_config import CHANNELS, load_chat_config, save_chat_config
from orac.credentials import CredentialStore
from orac.storage import BoardStore


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _ref_status(spec: dict[str, Any], creds: CredentialStore) -> dict[str, dict[str, Any]]:
    return {
        key: {"ref": str(spec[key]), "stored": creds.has(str(spec[key]))}
        for key in sorted(spec)
        if key.endswith("_ref")
    }


def _read_bridge_json(url: str, path: str, timeout: float = 0.35) -> dict[str, Any] | None:
    target = url.rstrip("/") + path
    req = Request(target, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read(256_000).decode("utf-8", errors="replace")
    except (OSError, URLError, ValueError, TimeoutError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}
    return data if isinstance(data, dict) else {"data": data}


def whatsapp_bridge_status(bridge_url: str) -> dict[str, Any]:
    """Best-effort localhost bridge probe.

    The Phase 3 bridge will own its exact API. This probe supports a small, local
    convention now: `/status` may return connection/session state, and `/qr` may
    return a QR payload. Absence of the bridge is a normal "not ready" state.
    """
    status = _read_bridge_json(bridge_url, "/status")
    qr = _read_bridge_json(bridge_url, "/qr") if status is not None else None
    connected = bool(status and status.get("connected"))
    return {
        "url": bridge_url,
        "reachable": status is not None,
        "connected": connected,
        "qr": (
            (qr or {}).get("qr")
            or (qr or {}).get("qr_png")
            or (qr or {}).get("data_url")
            or (status or {}).get("qr")
        ),
        "message": (
            str((status or {}).get("message"))
            if status and status.get("message")
            else ("Bridge reachable." if status is not None else "Bridge not reachable.")
        ),
    }


def chat_status(store: BoardStore) -> dict[str, Any]:
    creds = CredentialStore(store.root)
    cfg = load_chat_config(store)
    channels: dict[str, Any] = {}
    for channel in CHANNELS:
        spec = cfg["channels"][channel]
        entry = {
            "enabled": bool(spec.get("enabled")),
            "authorized_senders": list(spec.get("authorized_senders", [])),
            "credentials": _ref_status(spec, creds),
        }
        if channel == "whatsapp":
            entry["bridge_url"] = str(spec.get("bridge_url", "http://localhost:8788"))
            entry["bridge"] = whatsapp_bridge_status(entry["bridge_url"])
        channels[channel] = entry
    return {"enabled": bool(cfg.get("enabled")), "channels": channels}


def connect_slack(store: BoardStore, bot_token: str, app_token: str) -> dict[str, Any]:
    bot_token = _clean(bot_token)
    app_token = _clean(app_token)
    if not bot_token or not app_token:
        raise ValueError("Slack bot token and app token are required.")
    creds = CredentialStore(store.root)
    cfg = load_chat_config(store)
    spec = cfg["channels"]["slack"]
    creds.set(str(spec["bot_token_ref"]), bot_token)
    creds.set(str(spec["app_token_ref"]), app_token)
    spec["enabled"] = True
    cfg["enabled"] = True
    save_chat_config(store, cfg)
    return chat_status(store)


def prepare_whatsapp(
    store: BoardStore, bridge_url: str | None = None, session: str | None = None
) -> dict[str, Any]:
    cfg = load_chat_config(store)
    spec = cfg["channels"]["whatsapp"]
    bridge_url = _clean(bridge_url)
    session = _clean(session)
    if bridge_url:
        spec["bridge_url"] = bridge_url
    if session:
        CredentialStore(store.root).set(str(spec["session_ref"]), session)
        spec["enabled"] = True
        cfg["enabled"] = True
    else:
        bridge = whatsapp_bridge_status(str(spec.get("bridge_url", "http://localhost:8788")))
        if bridge.get("connected"):
            spec["enabled"] = True
            cfg["enabled"] = True
    save_chat_config(store, cfg)
    return chat_status(store)


def allow_sender(store: BoardStore, channel: str, sender: str) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ValueError(f"Unknown chat channel {channel!r}.")
    sender = _clean(sender)
    if not sender:
        raise ValueError("Sender id is required.")
    cfg = load_chat_config(store)
    spec = cfg["channels"][channel]
    senders = list(spec.get("authorized_senders", []))
    if sender not in senders:
        senders.append(sender)
    spec["authorized_senders"] = senders
    save_chat_config(store, cfg)
    return chat_status(store)


def disallow_sender(store: BoardStore, channel: str, sender: str) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ValueError(f"Unknown chat channel {channel!r}.")
    cfg = load_chat_config(store)
    spec = cfg["channels"][channel]
    sender = _clean(sender)
    spec["authorized_senders"] = [
        item for item in spec.get("authorized_senders", []) if item != sender
    ]
    save_chat_config(store, cfg)
    return chat_status(store)


def disconnect_channel(store: BoardStore, channel: str) -> dict[str, Any]:
    if channel not in CHANNELS:
        raise ValueError(f"Unknown chat channel {channel!r}.")
    creds = CredentialStore(store.root)
    cfg = load_chat_config(store)
    spec = cfg["channels"][channel]
    for key in [k for k in spec if k.endswith("_ref")]:
        creds.delete(str(spec[key]))
    spec["enabled"] = False
    save_chat_config(store, cfg)
    return chat_status(store)
