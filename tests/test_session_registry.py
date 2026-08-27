from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from orac.session_registry import (
    live_sessions,
    record_session_end,
    record_session_start,
    wip_advisory,
)


def test_record_session_start_and_end(tmp_path: Path) -> None:
    session_file = record_session_start(
        root=tmp_path, session_type="cli", pid=12345, cwd=tmp_path
    )
    assert session_file.is_file()

    data = json.loads(session_file.read_text(encoding="utf-8"))
    assert data["pid"] == 12345
    assert data["type"] == "cli"
    assert data["cwd"] == str(tmp_path.resolve())
    assert "started_at" in data

    record_session_end(session_file)
    assert not session_file.is_file()


def test_live_sessions_without_alive_check(tmp_path: Path) -> None:
    s1 = record_session_start(root=tmp_path, session_type="cli", pid=1001, cwd=tmp_path)
    s2 = record_session_start(root=tmp_path, session_type="daemon", pid=1002, cwd=tmp_path)

    sessions = live_sessions(root=tmp_path, check_alive=False)
    assert len(sessions) == 2
    pids = {s["pid"] for s in sessions}
    assert pids == {1001, 1002}


def test_prune_expired_sessions(tmp_path: Path) -> None:
    s1 = record_session_start(root=tmp_path, session_type="cli", pid=1001, cwd=tmp_path)
    # Manually backdate timestamp in s1
    data = json.loads(s1.read_text(encoding="utf-8"))
    data["ts"] = int(time.time()) - 5000
    s1.write_text(json.dumps(data), encoding="utf-8")

    # s2 is fresh
    s2 = record_session_start(root=tmp_path, session_type="cli", pid=1002, cwd=tmp_path)

    sessions = live_sessions(root=tmp_path, ttl_seconds=3600, check_alive=False)
    assert len(sessions) == 1
    assert sessions[0]["pid"] == 1002
    assert not s1.is_file()


def test_prune_dead_pids(tmp_path: Path) -> None:
    # PID 999999 is almost certainly non-existent
    s1 = record_session_start(root=tmp_path, session_type="cli", pid=999999, cwd=tmp_path)
    # Current process PID is definitely alive
    s2 = record_session_start(root=tmp_path, session_type="cli", pid=os.getpid(), cwd=tmp_path)

    sessions = live_sessions(root=tmp_path, check_alive=True)
    assert len(sessions) == 1
    assert sessions[0]["pid"] == os.getpid()
    assert not s1.is_file()


def test_wip_advisory_same_cwd_collision(tmp_path: Path) -> None:
    record_session_start(root=tmp_path, session_type="cli", pid=1001, cwd=tmp_path)
    record_session_start(root=tmp_path, session_type="daemon", pid=1002, cwd=tmp_path)

    advisories = wip_advisory(root=tmp_path, current_cwd=tmp_path, check_alive=False)
    assert len(advisories) == 1
    assert "[wip advisory]" in advisories[0]
    assert "2 active sessions" in advisories[0]
    assert "share working tree" in advisories[0]
    assert "concurrent Git branch/worktree mutations" in advisories[0]


def test_wip_advisory_different_cwd_no_collision(tmp_path: Path) -> None:
    dir1 = tmp_path / "repo1"
    dir2 = tmp_path / "repo2"
    dir1.mkdir()
    dir2.mkdir()

    record_session_start(root=tmp_path, session_type="cli", pid=1001, cwd=dir1)
    record_session_start(root=tmp_path, session_type="daemon", pid=1002, cwd=dir2)

    advisories = wip_advisory(root=tmp_path, current_cwd=dir1, check_alive=False)
    assert len(advisories) == 0


def test_wip_advisory_session_limit(tmp_path: Path) -> None:
    record_session_start(root=tmp_path, session_type="cli", pid=1001, cwd=tmp_path / "a")
    record_session_start(root=tmp_path, session_type="cli", pid=1002, cwd=tmp_path / "b")
    record_session_start(root=tmp_path, session_type="cli", pid=1003, cwd=tmp_path / "c")

    advisories = wip_advisory(
        root=tmp_path, current_cwd=tmp_path / "a", session_limit=2, check_alive=False
    )
    assert any("exceeds declared limit of 2" in adv for adv in advisories)
