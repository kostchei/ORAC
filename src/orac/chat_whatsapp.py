from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from orac.chat_config import load_chat_config
from orac.chat_gateway import ChatGateway, InboundMessage, OutboundMessage
from orac.storage import BoardStore


class WhatsAppConnectorError(RuntimeError):
    pass


class WhatsAppBridgeClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def status(self) -> dict[str, Any]:
        return self._json("GET", "/status")

    def messages(self) -> list[dict[str, Any]]:
        data = self._json("GET", "/messages")
        messages = data.get("messages", data)
        return messages if isinstance(messages, list) else []

    def send(self, target: str, text: str) -> None:
        self._json("POST", "/send", {"to": target, "text": text})

    def _json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(req, timeout=10) as response:
                raw = response.read(512_000).decode("utf-8", errors="replace")
        except (OSError, URLError) as exc:
            raise WhatsAppConnectorError(
                f"WhatsApp bridge is not reachable at {self.base_url}. "
                "Start it with `npm --prefix bridges/whatsapp install` then "
                "`npm --prefix bridges/whatsapp start`."
            ) from exc
        return json.loads(raw) if raw else {}


def run_whatsapp_connector(root: Path | str = ".", poll_interval: float = 2.0) -> None:
    store = BoardStore(root)
    cfg = load_chat_config(store)
    spec = cfg["channels"]["whatsapp"]
    if not cfg.get("enabled") or not spec.get("enabled"):
        raise WhatsAppConnectorError(
            "WhatsApp channel is not enabled. Pair it and allow your phone in the local sign-on box."
        )
    client = WhatsAppBridgeClient(str(spec.get("bridge_url", "http://localhost:8788")))
    status = client.status()
    if not status.get("connected"):
        raise WhatsAppConnectorError("WhatsApp bridge is reachable but not paired yet. Scan the QR first.")

    gateway = ChatGateway(store.root)
    while True:
        for item in client.messages():
            sender = str(item.get("sender") or "")
            text = str(item.get("text") or "")
            for reply in gateway.handle(InboundMessage("whatsapp", sender, text)):
                client.send(reply.target, reply.text)
        for outbound in gateway.poll_outbound():
            if outbound.channel == "whatsapp":
                _send_whatsapp(client, outbound)
        time.sleep(poll_interval)


def _send_whatsapp(client: WhatsAppBridgeClient, outbound: OutboundMessage) -> None:
    client.send(outbound.target, outbound.text)
