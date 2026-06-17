from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

from orac.adapters import Adapter
from orac.chat_config import load_chat_config
from orac.credentials import CredentialError, CredentialStore
from orac.models import CapabilityRequest
from orac.storage import BoardStore
from orac.tooling import ToolResult

# Channel-agnostic comms adapters (Group 2). Reads, drafts, and sends messages on
# Slack and WhatsApp, routed to mockable injected backends so the broker path is
# testable without a live network.
#
# The governance split lives in policy.py, not here:
#   - channel.read / channel.draft  -> reversible + local  -> auto (audit only)
#   - channel.send                  -> IRREVERSIBLE + external -> APPROVE (parks
#     for a human before it executes)
#
# That last line is the whole point: a sent message cannot be unsent, so the gate
# sits *before* the send (human approval), not after (there is no rollback). This
# implementation does not fabricate an "inverse operation" for channel.send — an
# honest irreversible action carries no rollback contract. Its reversibility is
# the human approval that precedes it, and the audit log is the record that it
# happened.

COMMS_TOOLS = frozenset({"channel.read", "channel.draft", "channel.send"})

_VALID_CHANNELS = ("slack", "whatsapp")


class CommsBackend(Protocol):
    def read(self, target: str) -> list[dict[str, Any]]: ...

    def send(self, target: str, text: str) -> str: ...


def _safe_slug(value: str) -> str:
    """A filesystem-safe token for a channel target (phone number, channel id).

    Targets are externally supplied (a phone number, a Slack id) and must never
    steer the draft path. Anything outside ``[A-Za-z0-9._-]`` is collapsed, so a
    target like ``../../etc`` cannot escape the drafts directory.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return slug or "unknown"


class RealSlackBackend:
    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token

    def _client(self) -> Any:
        try:
            from slack_sdk import WebClient
        except ImportError as exc:  # fail loud: a missing optional dep is a fault
            raise RuntimeError(
                "Slack backend requires slack_sdk (pip install -e .[chat])."
            ) from exc
        return WebClient(token=self.bot_token)

    def _channel_id(self, client: Any, target: str) -> str:
        if target.startswith(("C", "D", "G")):
            return target
        return client.conversations_open(users=target)["channel"]["id"]

    def read(self, target: str) -> list[dict[str, Any]]:
        client = self._client()
        res = client.conversations_history(
            channel=self._channel_id(client, target), limit=10
        )
        return [
            {
                "sender": msg.get("user", ""),
                "text": msg.get("text", ""),
                "ts": msg.get("ts", ""),
            }
            for msg in res.get("messages", [])
            if not msg.get("subtype") and not msg.get("bot_id")
        ]

    def send(self, target: str, text: str) -> str:
        client = self._client()
        res = client.chat_postMessage(channel=self._channel_id(client, target), text=text)
        return str(res.get("ts") or "")


class RealWhatsAppBackend:
    def __init__(self, bridge_url: str) -> None:
        self.bridge_url = bridge_url

    def _client(self) -> Any:
        from orac.chat_whatsapp import WhatsAppBridgeClient

        return WhatsAppBridgeClient(self.bridge_url)

    def read(self, target: str) -> list[dict[str, Any]]:
        client = self._client()
        return [
            {"sender": msg.get("sender", ""), "text": msg.get("text", "")}
            for msg in client.messages()
            if msg.get("sender") == target
        ]

    def send(self, target: str, text: str) -> str:
        self._client().send(target, text)
        return "ok"


class CommsAdapterSet:
    """Comms adapters bound to a repo root, with optionally injected backends.

    Backends default to the real Slack/WhatsApp clients, built lazily from the
    chat config + credential vault and failing closed when credentials are
    absent. Tests inject fakes so the governed path runs offline.
    """

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

    # --- backend resolution (fail-closed) ---------------------------------

    def _get_backend(self, channel: str) -> CommsBackend:
        if channel == "slack":
            if self._slack is not None:
                return self._slack
            spec = self._channel_spec("slack")
            token = CredentialStore(self.root).get(
                spec.get("bot_token_ref", "chat.slack.bot_token")
            )
            if not token:
                raise CredentialError("Slack credentials are not configured.")
            return RealSlackBackend(token)

        if channel == "whatsapp":
            if self._whatsapp is not None:
                return self._whatsapp
            spec = self._channel_spec("whatsapp")
            session = CredentialStore(self.root).get(
                spec.get("session_ref", "chat.whatsapp.session")
            )
            if not session:
                raise CredentialError("WhatsApp credentials are not configured.")
            return RealWhatsAppBackend(spec.get("bridge_url", "http://localhost:8788"))

        raise ValueError(
            f"Unknown channel {channel!r}; expected one of {_VALID_CHANNELS}."
        )

    def _channel_spec(self, channel: str) -> dict[str, Any]:
        cfg = load_chat_config(BoardStore(self.root))
        return cfg.get("channels", {}).get(channel, {})

    def _drafts_dir(self) -> Path:
        out = (self.root / ".orac" / "outputs" / "comms").resolve()
        out.mkdir(parents=True, exist_ok=True)
        return out

    # --- adapters ---------------------------------------------------------

    def channel_read(self, req: CapabilityRequest) -> ToolResult:
        channel = req.args["channel"]
        target = req.args["target"]
        messages = self._get_backend(channel).read(target)
        return ToolResult(
            "channel.read",
            f"Read {len(messages)} message(s) from {channel}:{target}.",
            {"channel": channel, "target": target, "messages": messages},
        )

    def channel_draft(self, req: CapabilityRequest) -> ToolResult:
        """Record a proposed message as a reviewable artifact — no send.

        Draft is the always-reversible half of comms: the doer writes what it
        intends to say so a human can read it before the (gated) send.
        """
        channel = req.args["channel"]
        target = req.args["target"]
        text = req.args["text"]
        if channel not in _VALID_CHANNELS:
            raise ValueError(
                f"Unknown channel {channel!r}; expected one of {_VALID_CHANNELS}."
            )
        drafts = self._drafts_dir()
        draft_file = (drafts / f"{channel}_{_safe_slug(target)}.txt").resolve()
        if drafts not in draft_file.parents:  # defence in depth over _safe_slug
            raise PermissionError(f"Draft path {draft_file} escapes {drafts}.")
        draft_file.write_text(text, encoding="utf-8")
        return ToolResult(
            "channel.draft",
            f"Recorded draft for {channel}:{target} ({draft_file.name}).",
            {
                "channel": channel,
                "target": target,
                "text": text,
                "path": str(draft_file),
            },
        )

    def channel_send(self, req: CapabilityRequest) -> ToolResult:
        """Send a message. Irreversible + external — gated by human APPROVE.

        By the time this adapter runs the broker has already cleared the human
        approval the risk model demands. There is no rollback contract: the send
        cannot be undone, which is exactly why the gate precedes it.
        """
        channel = req.args["channel"]
        target = req.args["target"]
        text = req.args["text"]
        message_id = self._get_backend(channel).send(target, text)
        return ToolResult(
            "channel.send",
            f"Sent message to {channel}:{target} (id: {message_id}).",
            {
                "channel": channel,
                "target": target,
                "text": text,
                "message_id": message_id,
            },
        )


def comms_adapters_for(
    repo_root: Path | str,
    slack_backend: CommsBackend | None = None,
    whatsapp_backend: CommsBackend | None = None,
) -> dict[str, Adapter]:
    return CommsAdapterSet(repo_root, slack_backend, whatsapp_backend).adapters()
