from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orac.models import Board, now_iso


class BoardStore:
    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root)
        self.state_dir = self.root / ".orac"
        self.board_path = self.state_dir / "board.json"
        self.config_path = self.state_dir / "config.json"
        self.usage_path = self.state_dir / "usage.json"

    def init(self) -> Board:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.board_path.exists():
            return self.load()
        board = Board()
        self.save(board)
        return board

    def load(self) -> Board:
        if not self.board_path.exists():
            raise FileNotFoundError(
                f"No ORAC board found at {self.board_path}. Run `orac init` first."
            )
        data = json.loads(self.board_path.read_text(encoding="utf-8"))
        return Board.from_dict(data)

    def save(self, board: Board) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        board.updated_at = now_iso()
        payload = json.dumps(board.to_dict(), indent=2, sort_keys=True)
        self.board_path.write_text(payload + "\n", encoding="utf-8")

    def load_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return dict(default)
        return json.loads(path.read_text(encoding="utf-8"))

    def save_json(self, path: Path, data: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True)
        path.write_text(payload + "\n", encoding="utf-8")
