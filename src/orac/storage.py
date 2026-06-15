from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, TypeVar

from orac.board_merge import merge_boards
from orac.models import Board, now_iso

if os.name == "nt":
    import msvcrt
else:
    import fcntl

T = TypeVar("T")


def _board_change_summary(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[str]]:
    """Which task ids were added / updated / removed between two board states.

    Informational only — for a readable event history. The full snapshot in each
    event is what rebuild relies on, never this summary.
    """
    old_tasks = {t["id"]: t for t in old.get("tasks", []) if "id" in t}
    new_tasks = {t["id"]: t for t in new.get("tasks", []) if "id" in t}
    return {
        "added": [i for i in new_tasks if i not in old_tasks],
        "updated": [i for i in new_tasks if i in old_tasks and new_tasks[i] != old_tasks[i]],
        "removed": [i for i in old_tasks if i not in new_tasks],
    }


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
        # Append-only log of every committed board state (one JSON event per line).
        self.events_path = self.state_dir / "board.events.jsonl"

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
            old_data: dict[str, Any] = {}
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
                old_data = current
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
            # Append the committed state to the event log AFTER board.json is
            # durable, inside the same lock so events are totally ordered and the
            # log can never get ahead of the authoritative board.
            self._append_event(old_data, data)

    def update(self, mutate: Callable[[Board], T], *, retries: int = 5) -> T:
        """Load → mutate → save, reapplying ``mutate`` to a freshly reloaded
        board if a concurrent writer bumped the revision in between.

        The recovery path (beyond raising ``StaleBoardError``) for writers whose
        mutation is a *pure function* of the board — add a task, change a field,
        ack a slice. Reapplying against the current board is exact, so the writer
        always lands without clobbering the concurrent update. Bounded by
        ``retries``; the last conflict is re-raised if the budget is exhausted.
        """
        error: StaleBoardError | None = None
        for _ in range(retries + 1):
            board = self.load()
            result = mutate(board)
            try:
                self.save(board)
                return result
            except StaleBoardError as exc:
                error = exc
        assert error is not None  # the loop only exits early on success
        raise error

    def save_merging(
        self, board: Board, base: Board | None = None, *, retries: int = 5
    ) -> list[str]:
        """Save ``board``; on a concurrent-write conflict, three-way merge it
        against the current on-disk board and retry. Returns the task ids that
        were genuine merge conflicts (resolved by newest update, never dropped).

        The recovery path for writers whose mutation *cannot* simply be reapplied
        — the daemon tick runs a long, side-effecting Scrum cycle that must not be
        re-executed. ``base`` is the board as loaded *before* the mutation (the
        common ancestor); if omitted it is recovered from the event log at
        ``board.revision``. The merge is task-level: the daemon's in-flight tasks
        and a concurrent writer's new task are disjoint and union cleanly.
        """
        ours = board
        candidate = board
        conflicts: list[str] = []
        for attempt in range(retries + 1):
            try:
                self.save(candidate)
                return conflicts
            except StaleBoardError:
                if attempt >= retries:
                    raise
                ancestor = base if base is not None else self._board_at_revision(ours.revision)
                if ancestor is None:
                    raise  # no common base to merge against — fail closed
                theirs = self.load()
                merge = merge_boards(ancestor, ours, theirs)
                conflicts = merge.conflicts
                candidate = merge.board  # revision == theirs.revision, so the next CAS matches
        return conflicts  # unreachable: the loop returns or raises

    def _board_at_revision(self, revision: int) -> Board | None:
        """The board snapshot at ``revision`` from the event log, or None."""
        for event in self.read_events():
            if int(event.get("revision", -1)) == revision:
                return Board.from_dict(event["board"])
        return None

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

    def _append_event(self, old_data: dict[str, Any], new_data: dict[str, Any]) -> None:
        """Append one committed board state to the append-only event log.

        Each line is a self-contained event: a full board SNAPSHOT (authoritative
        for rebuild — replay is trivially the latest snapshot, so there is no
        replay-inequality risk) plus a human-readable change summary (which task
        ids were added / updated / removed since the previous commit). Append +
        fsync per line, so a crash can only ever leave a torn FINAL line, which
        readers skip. The log is a secondary record: board.json stays the
        authoritative current state; the log is full history + a recovery source
        stronger than the single last-good backup.
        """
        revision = int(new_data.get("revision", 0))
        event = {
            "seq": revision,
            "ts": new_data.get("updated_at") or now_iso(),
            "revision": revision,
            "tasks": len(new_data.get("tasks", [])),
            "changes": _board_change_summary(old_data, new_data),
            "board": new_data,
        }
        line = json.dumps(event, sort_keys=True) + "\n"
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def read_events(self) -> list[dict[str, Any]]:
        """All board events in commit order. Tolerant of a torn final line (a
        crash mid-append): unparseable lines are skipped, never raised on, since
        only the last line can be partial."""
        if not self.events_path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def rebuild_from_events(self) -> Board:
        """Reconstruct the board from the event log's latest committed snapshot.

        Recovers even when both board.json and the last-good backup are lost or
        corrupt (the log is the full history). Recovers to the last FULLY-LOGGED
        state: if a crash landed between the board.json write and the event append,
        this returns the prior revision (board.json itself, if intact, is newer)."""
        events = self.read_events()
        if not events:
            raise FileNotFoundError(
                f"No board events at {self.events_path} to rebuild from."
            )
        latest = max(events, key=lambda e: int(e.get("revision", 0)))
        return Board.from_dict(latest["board"])

    def restore_from_events(self) -> Board:
        """Rebuild from the event log and write it back as the current board."""
        with _BoardLock(self.lock_path):
            events = self.read_events()
            if not events:
                raise FileNotFoundError(
                    f"No board events at {self.events_path} to rebuild from."
                )
            latest = max(events, key=lambda e: int(e.get("revision", 0)))
            payload = json.dumps(latest["board"], indent=2, sort_keys=True) + "\n"
            self._save_atomic(self.board_path, payload)
            self._save_atomic(self.backup_path, payload)
            return Board.from_dict(latest["board"])

    def load_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return dict(default)
        return json.loads(path.read_text(encoding="utf-8"))

    def save_json(self, path: Path, data: dict[str, Any]) -> None:
        payload = json.dumps(data, indent=2, sort_keys=True)
        self._save_atomic(path, payload + "\n")
