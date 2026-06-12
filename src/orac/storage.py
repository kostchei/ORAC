from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from orac.models import Board, now_iso

if os.name == "nt":
    import msvcrt
else:
    import fcntl


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


class StaleBoardError(RuntimeError):
    """The board on disk changed after this Board was loaded. Saving it would
    silently destroy the other writer's updates; reload and reapply instead."""

    def __init__(
        self, path: Path, loaded_revision: int, current_revision: int
    ) -> None:
        self.path = path
        self.loaded_revision = loaded_revision
        self.current_revision = current_revision
        super().__init__(
            f"Board {path} is at revision {current_revision} but this save is "
            f"based on revision {loaded_revision}: another process wrote the "
            "board since it was loaded. Reload the board and reapply the change."
        )


class _BoardLock:
    """Exclusive inter-process lock for the board's check-and-swap critical
    section. Backed by an OS file lock (msvcrt/fcntl), which the kernel
    releases automatically when the holding process dies — there are no stale
    locks to clean up. Not reentrant: save() and recover() acquire it
    internally, so callers must never hold it around those calls."""

    def __init__(self, path: Path, timeout_seconds: float = 10.0) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle: Any = None

    def __enter__(self) -> "_BoardLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(
                        f"Could not acquire board lock {self.path} within "
                        f"{self.timeout_seconds}s; another ORAC process holds it."
                    ) from None
                time.sleep(0.05)

    def __exit__(self, *exc_info: object) -> None:
        handle = self._handle
        self._handle = None
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class BoardStore:
    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root)
        self.state_dir = self.root / ".orac"
        self.board_path = self.state_dir / "board.json"
        self.backup_path = self.state_dir / "board.last-good.json"
        self.lock_path = self.state_dir / "board.lock"
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
                f.flush()
                # Force data to disk before the rename: without this, a power
                # loss can land the rename ahead of the data and leave a
                # valid-looking but truncated file.
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        except Exception:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise

    def save(self, board: Board) -> None:
        # Lock + revision check: two concurrent load-modify-save writers must
        # not silently last-writer-win. The lock makes the check-and-swap
        # atomic across processes; the revision check turns a lost update into
        # a loud StaleBoardError instead of quiet data loss.
        with _BoardLock(self.lock_path):
            current_revision = 0
            if self.board_path.exists():
                try:
                    current = json.loads(
                        self.board_path.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError as exc:
                    # Fail closed: never paper over a corrupt board with a
                    # blind overwrite — recover it explicitly first.
                    raise CorruptStateError(
                        self.board_path, self.backup_path, exc
                    ) from exc
                current_revision = int(current.get("revision", 0))
                if board.revision != current_revision:
                    raise StaleBoardError(
                        self.board_path, board.revision, current_revision
                    )
            board.updated_at = now_iso()
            data = board.to_dict()
            # Only adopt the new revision in memory once the write lands, so a
            # failed save leaves the board retryable at its loaded revision.
            data["revision"] = current_revision + 1
            payload = json.dumps(data, indent=2, sort_keys=True)
            self._save_atomic(self.board_path, payload + "\n")
            self._save_atomic(self.backup_path, payload + "\n")
            board.revision = current_revision + 1

    def recover(self) -> Board:
        with _BoardLock(self.lock_path):
            if not self.backup_path.exists():
                raise FileNotFoundError(
                    f"No ORAC board backup found at {self.backup_path}."
                )
            try:
                payload = self.backup_path.read_text(encoding="utf-8")
                board = Board.from_dict(json.loads(payload))
            except json.JSONDecodeError as exc:
                raise CorruptStateError(
                    self.backup_path, self.backup_path, exc
                ) from exc
            self._save_atomic(self.board_path, payload)
            return board

    def load_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return dict(default)
        return json.loads(path.read_text(encoding="utf-8"))

    def save_json(self, path: Path, data: dict[str, Any]) -> None:
        payload = json.dumps(data, indent=2, sort_keys=True)
        self._save_atomic(path, payload + "\n")
