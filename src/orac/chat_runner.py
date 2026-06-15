from __future__ import annotations

import threading
import time
from pathlib import Path

from orac.chat_config import load_chat_config
from orac.chat_slack import run_slack_connector
from orac.chat_whatsapp import run_whatsapp_connector
from orac.storage import BoardStore


def run_chat_connectors(
    root: Path | str = ".",
    *,
    slack: bool = True,
    whatsapp: bool = True,
    poll_interval: float = 3.0,
) -> None:
    cfg = load_chat_config(BoardStore(root))
    slack = slack and bool(cfg.get("enabled")) and bool(cfg["channels"]["slack"].get("enabled"))
    whatsapp = whatsapp and bool(cfg.get("enabled")) and bool(
        cfg["channels"]["whatsapp"].get("enabled")
    )
    threads: list[threading.Thread] = []
    errors: list[str] = []

    def start(name: str, target) -> None:  # type: ignore[no-untyped-def]
        def runner() -> None:
            try:
                target(root, poll_interval=poll_interval)
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        thread = threading.Thread(target=runner, daemon=True, name=f"orac-{name}")
        thread.start()
        threads.append(thread)

    if slack:
        start("slack", run_slack_connector)
    if whatsapp:
        start("whatsapp", run_whatsapp_connector)
    if not threads:
        raise ValueError("No chat connectors selected.")

    while True:
        if errors:
            raise RuntimeError("; ".join(errors))
        if not any(thread.is_alive() for thread in threads):
            raise RuntimeError("All chat connector threads stopped.")
        time.sleep(1)
