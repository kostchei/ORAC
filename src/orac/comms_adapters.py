from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Protocol

from orac.adapters import Adapter
from orac.chat_config import load_chat_config
from orac.credentials import CredentialError, CredentialStore
from orac.models import CapabilityRequest
from orac.storage import BoardStore
from orac.tooling import ToolResult

# Channel-agnostic comms adapters.
#
# Register under the broker; these adapters route Slack and WhatsApp reads, drafts,
# and sends to mockable, injected backends. They fail closed unless credentials are
# explicitly configured in the CredentialStore.

COMMS_TOOLS = frozenset({"channel.read", "channel.draft", "channel.send"})


class CommsBackend(Protocol):
    def read(self, target: str) -> list[dict[str, Any]]:
        ...

    def send(self, target: str, text: str) -> str:
        ...


class RealSlackBackend:
    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token

    def _client(self) -> Any:
        try:
            from slack_sdk import WebClient
        except ImportError as exc:
            raise RuntimeError(
                "Slack backend requires slack_sdk. Install it via pip install -e .[chat]"
            ) from exc
        return WebClient(token=self.bot_token)

    def read(self, target: str) -> list[dict[str, Any]]:
        client = self._client()
        channel = target
        if not channel.startswith(("C", "D", "G")):
            opened = client.conversations_open(users=target)
            channel = opened["channel"]["id"]
        res = client.conversations_history(channel=channel, limit=10)
        messages = []
        for msg in res.get("messages", []):
            if not msg.get("subtype") and not msg.get("bot_id"):
                messages.append({
                    "sender": msg.get("user", ""),
                    "text": msg.get("text", ""),
                    "ts": msg.get("ts", ""),
                })
        return messages

    def send(self, target: str, text: str) -> str:
        client = self._client()
        channel = target
        if not channel.startswith(("C", "D", "G")):
            opened = client.conversations_open(users=target)
            channel = opened["channel"]["id"]
        res = client.chat_postMessage(channel=channel, text=text)
        return str(res.get("ts") or "")


class RealWhatsAppBackend:
    def __init__(self, bridge_url: str) -> None:
        self.bridge_url = bridge_url

    def _client(self) -> Any:
        from orac.chat_whatsapp import WhatsAppBridgeClient
        return WhatsAppBridgeClient(self.bridge_url)

    def read(self, target: str) -> list[dict[str, Any]]:
        client = self._client()
        all_msgs = client.messages()
        return [
            {
                "sender": msg.get("sender", ""),
                "text": msg.get("text", ""),
            }
            for msg in all_msgs
            if msg.get("sender") == target
        ]

    def send(self, target: str, text: str) -> str:
        client = self._client()
        client.send(target, text)
        return "ok"


class CommsAdapterSet:
    def __init__(
        self,
        repo_root: Path | str,
        slack_backend: CommsBackend | None = None,
        whatsapp_backend: CommsBackend | None = None,
    ) -> None:
        self.root = Path(repo_root)
        self._slack = slack_backend
        self._whatsapp = whatsapp_backend

    def adapters(self) -> dict[str, Adapter]:
        return {
            "channel.read": self.channel_read,
            "channel.draft": self.channel_draft,
            "channel.send": self.channel_send,
        }

    def _get_backend(self, channel: str) -> CommsBackend:
        if channel == "slack":
            if self._slack is not None:
                return self._slack
            store = BoardStore(self.root)
            cfg = load_chat_config(store)
            spec = cfg["channels"].get("slack", {})
            bot_token_ref = spec.get("bot_token_ref", "chat.slack.bot_token")
            bot_token = CredentialStore(self.root).get(bot_token_ref)
            if not bot_token:
                raise CredentialError("Slack credentials are not configured.")
            return RealSlackBackend(bot_token)

        if channel == "whatsapp":
            if self._whatsapp is not None:
                return self._whatsapp
            store = BoardStore(self.root)
            cfg = load_chat_config(store)
            spec = cfg["channels"].get("whatsapp", {})
            session_ref = spec.get("session_ref", "chat.whatsapp.session")
            session = CredentialStore(self.root).get(session_ref)
            if not session:
                raise CredentialError("WhatsApp credentials are not configured.")
            bridge_url = spec.get("bridge_url", "http://localhost:8788")
            return RealWhatsAppBackend(bridge_url)

        raise ValueError(f"Unknown channel {channel!r}; expected 'slack' or 'whatsapp'.")

    def channel_read(self, req: CapabilityRequest) -> ToolResult:
        channel = req.args["channel"]
        target = req.args["target"]
        backend = self._get_backend(channel)
        messages = backend.read(target)
        return ToolResult(
            name="channel.read",
            message=f"Read {len(messages)} messages from {channel}:{target}.",
            data={"channel": channel, "target": target, "messages": messages},
        )

    def channel_draft(self, req: CapabilityRequest) -> ToolResult:
        channel = req.args["channel"]
        target = req.args["target"]
        text = req.args["text"]
        out_dir = self.root / ".orac" / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        draft_file = out_dir / f"draft_{channel}_{target}.txt"
        draft_file.write_text(text, encoding="utf-8")

        return ToolResult(
            name="channel.draft",
            message=f"Recorded draft message for {channel}:{target} to {draft_file.name}.",
            data={"channel": channel, "target": target, "text": text, "path": str(draft_file)},
        )

    def channel_send(self, req: CapabilityRequest) -> ToolResult:
        channel = req.args["channel"]
        target = req.args["target"]
        text = req.args["text"]
        backend = self._get_backend(channel)
        msg_id = backend.send(target, text)

        contract = {
            "target_resource": f"{channel}:{target}",
            "idempotency_key": uuid.uuid4().hex,
            "inverse_operation": {
                "operation": "channel.post_correction",
                "state_before": {
                    "channel": channel,
                    "target": target,
                    "text": text,
                },
            },
        }

        return ToolResult(
            name="channel.send",
            message=f"Sent message to {channel}:{target} (msg_id: {msg_id}).",
            data={
                "channel": channel,
                "target": target,
                "text": text,
                "message_id": msg_id,
                "rollback_contract": contract,
            },
        )


def comms_adapters_for(
    repo_root: Path | str,
    slack_backend: CommsBackend | None = None,
    whatsapp_backend: CommsBackend | None = None,
) -> dict[str, Adapter]:
    return CommsAdapterSet(repo_root, slack_backend, whatsapp_backend).adapters()
