from __future__ import annotations

import threading
import time
from pathlib import Path

from orac.chat_config import load_chat_config
from orac.chat_gateway import ChatGateway, InboundMessage, OutboundMessage
from orac.credentials import CredentialStore
from orac.storage import BoardStore


class SlackConnectorError(RuntimeError):
    pass


def _load_slack_deps():
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as exc:
        raise SlackConnectorError(
            "Slack connector needs the chat optional dependency. Install it with "
            "`pip install -e .[chat]` from the ORAC repo."
        ) from exc
    return App, SocketModeHandler


def run_slack_connector(root: Path | str = ".", poll_interval: float = 5.0) -> None:
    App, SocketModeHandler = _load_slack_deps()
    store = BoardStore(root)
    cfg = load_chat_config(store)
    spec = cfg["channels"]["slack"]
    creds = CredentialStore(store.root)
    bot_token = creds.get(str(spec["bot_token_ref"]))
    app_token = creds.get(str(spec["app_token_ref"]))
    if not cfg.get("enabled") or not spec.get("enabled"):
        raise SlackConnectorError("Slack channel is not enabled. Use the local sign-on box first.")
    if not bot_token or not app_token:
        raise SlackConnectorError("Slack bot/app tokens are missing. Use the local sign-on box first.")

    gateway = ChatGateway(store.root)
    app = App(token=bot_token)

    @app.event("message")
    def _handle_message(event, say):  # type: ignore[no-untyped-def]
        if event.get("subtype") or event.get("bot_id"):
            return
        sender = str(event.get("user") or "")
        text = str(event.get("text") or "")
        reply_to = str(event.get("channel") or "")
        for reply in gateway.handle(
            InboundMessage(channel="slack", sender=sender, text=text, reply_to=reply_to)
        ):
            say(text=reply.text)

    client = app.client

    def _push_loop() -> None:
        while True:
            for outbound in gateway.poll_outbound():
                if outbound.channel == "slack":
                    _post_slack(client, outbound)
            time.sleep(poll_interval)

    threading.Thread(target=_push_loop, daemon=True).start()
    SocketModeHandler(app, app_token).start()


def _post_slack(client, outbound: OutboundMessage) -> None:  # type: ignore[no-untyped-def]
    channel = outbound.target
    if not channel.startswith(("C", "D", "G")):
        opened = client.conversations_open(users=channel)
        channel = opened["channel"]["id"]
    client.chat_postMessage(channel=channel, text=outbound.text)
