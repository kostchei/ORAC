from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orac.models import now_iso


class CommsLog:
    """Local append-only comms log under `.orac/comms_logs/`."""

    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root)
        self.dir = self.root / ".orac" / "comms_logs"
        self.messages_path = self.dir / "messages.jsonl"

    def record(self, kind: str, **data: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        event = {"ts": now_iso(), "kind": kind, **data}
        with open(self.messages_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True, default=str) + "\n")
