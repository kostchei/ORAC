from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from orac.models import Board, now_iso


class CorruptStateError(RuntimeError):
    """Raised when persisted ORAC state cannot be decoded."""

    def __init__(self, path: Path, backup_path: Path, cause: Exception) -> None:
        self.path = path
        self.backup_path = backup_path
        super().__init__(
            f"Corrupt ORAC state at {path}. "
            f"Run `orac board recover` to restore from {backup_path}."
        )
        self.__cause__ = cause


class BoardStore:
    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root)
        self.state_dir = self.root / ".orac"
        self.board_path = self.state_dir / "board.json"
        self.backup_path = self.state_dir / "board.last-good.json"
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
        try:
            data = json.loads(self.board_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CorruptStateError(self.board_path, self.backup_path, exc) from exc
        return Board.from_dict(data)

    def _save_atomic(self, path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path_str = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
            text=True,
        )
        temp_path = Path(temp_path_str)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(temp_path, path)
        except Exception:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise

    def save(self, board: Board) -> None:
        board.updated_at = now_iso()
        payload = json.dumps(board.to_dict(), indent=2, sort_keys=True)
        self._save_atomic(self.board_path, payload + "\n")
        self._save_atomic(self.backup_path, payload + "\n")

    def recover(self) -> Board:
        if not self.backup_path.exists():
            raise FileNotFoundError(
                f"No ORAC board backup found at {self.backup_path}."
            )
        try:
            payload = self.backup_path.read_text(encoding="utf-8")
            board = Board.from_dict(json.loads(payload))
        except json.JSONDecodeError as exc:
            raise CorruptStateError(self.backup_path, self.backup_path, exc) from exc
        self._save_atomic(self.board_path, payload)
        return board

    def load_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return dict(default)
        return json.loads(path.read_text(encoding="utf-8"))

    def save_json(self, path: Path, data: dict[str, Any]) -> None:
        payload = json.dumps(data, indent=2, sort_keys=True)
        self._save_atomic(path, payload + "\n")
