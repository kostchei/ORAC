from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orac.models import now_iso

# Default session TTL (in seconds) for stale cleanup fallback
DEFAULT_SESSION_TTL_SECONDS = 86400  # 24 hours


def _sessions_dir(root: Path | str = ".") -> Path:
    s_dir = Path(root) / ".orac" / "sessions"
    s_dir.mkdir(parents=True, exist_ok=True)
    return s_dir


def _is_process_alive(pid: int) -> bool:
    """Check if process with given PID is still alive. Robust & cross-platform."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
            )
            if handle == 0:
                return False
            exit_code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            STILL_ACTIVE = 259
            return exit_code.value == STILL_ACTIVE
        except Exception:
            return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def record_session_start(
    root: Path | str = ".",
    session_type: str = "cli",
    pid: int | None = None,
    cwd: Path | str | None = None,
) -> Path:
    """Record an active session in .orac/sessions/<pid>-<timestamp>.json."""
    s_dir = _sessions_dir(root)
    actual_pid = pid if pid is not None else os.getpid()
    actual_cwd = str(Path(cwd or os.getcwd()).resolve())
    ts = int(time.time())
    session_file = s_dir / f"{actual_pid}-{ts}.json"
    data = {
        "pid": actual_pid,
        "type": session_type,
        "cwd": actual_cwd,
        "started_at": now_iso(),
        "ts": ts,
    }
    session_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return session_file


def record_session_end(session_file: Path | str | None) -> None:
    """Remove a session record when the session completes."""
    if session_file is None:
        return
    path = Path(session_file)
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def live_sessions(
    root: Path | str = ".",
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    check_alive: bool = True,
) -> list[dict[str, Any]]:
    """Return all active sessions, pruning dead PIDs and expired files."""
    s_dir = _sessions_dir(root)
    sessions: list[dict[str, Any]] = []
    now_ts = int(time.time())

    for path in s_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                path.unlink(missing_ok=True)
                continue

            pid = int(data.get("pid", 0))
            ts = int(data.get("ts", 0))

            # Prune if expired by TTL
            if ts > 0 and (now_ts - ts) > ttl_seconds:
                path.unlink(missing_ok=True)
                continue

            # Prune if process is no longer alive
            if check_alive and pid > 0 and not _is_process_alive(pid):
                path.unlink(missing_ok=True)
                continue

            sessions.append(data)
        except Exception:
            # Corrupted or unreadable session file -> prune
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    return sessions


def _load_wip_config(root: Path | str = ".") -> dict[str, Any]:
    config_path = Path(root) / ".orac" / "config.json"
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data.get("wip", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}


def wip_advisory(
    root: Path | str = ".",
    current_cwd: Path | str | None = None,
    session_limit: int | None = None,
    check_alive: bool = True,
) -> list[str]:
    """Check active sessions for concurrent-worktree collisions and session limit.

    W1: Read-only relative to broker governance; local session files only.
    W2: Advisory-only; returns warning strings, never blocks.
    W3: Honesty discipline; reports observed live sessions and matching working trees.
    """
    sessions = live_sessions(root, check_alive=check_alive)
    advisories: list[str] = []

    # 1. Check same-worktree collisions
    target_cwd = str(Path(current_cwd or os.getcwd()).resolve())
    same_cwd_sessions = [
        s for s in sessions if Path(str(s.get("cwd", ""))).resolve() == Path(target_cwd).resolve()
    ]
    if len(same_cwd_sessions) > 1:
        pids = sorted(s.get("pid") for s in same_cwd_sessions if s.get("pid"))
        types = [s.get("type", "session") for s in same_cwd_sessions]
        advisories.append(
            f"[wip advisory] {len(same_cwd_sessions)} active sessions ({', '.join(types)} "
            f"PIDs: {pids}) share working tree '{target_cwd}'. "
            "Risk: concurrent Git branch/worktree mutations."
        )

    # 2. Check total session limit if declared
    cfg = _load_wip_config(root)
    limit = session_limit if session_limit is not None else cfg.get("session_limit")
    if limit is not None and int(limit) > 0:
        limit_val = int(limit)
        if len(sessions) > limit_val:
            advisories.append(
                f"[wip advisory] {len(sessions)} active session(s) exceeds declared limit of {limit_val}."
            )

    return advisories
