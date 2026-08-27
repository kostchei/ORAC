from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orac.broker_store import BrokerStore
from orac.models import CapabilityRequest, CapabilityResult, CapabilityStatus
from orac.notify import (
    RESERVOIR_COUNT_THRESHOLD_DEFAULT,
    RESERVOIR_SPAN_MINUTES_DEFAULT,
    reservoir_advisory,
)


def _record_human_action(
    store: BrokerStore,
    timestamp: datetime,
    tool: str = "queue.ack",
    task_id: str = "t1",
) -> None:
    iso = timestamp.isoformat()
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO audit (created_at, agent, tool, task_id, status, message, args_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iso, "human", tool, task_id, "allowed", "human action", "{}"),
        )


def _record_agent_action(
    store: BrokerStore,
    timestamp: datetime,
    agent: str = "Builder",
    tool: str = "repo.write_file",
) -> None:
    iso = timestamp.isoformat()
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO audit (created_at, agent, tool, task_id, status, message, args_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iso, agent, tool, "t1", "allowed", "agent action", "{}"),
        )


def test_empty_store_returns_none(tmp_path: Path) -> None:
    store = BrokerStore(tmp_path).init()
    assert reservoir_advisory(store) is None


def test_below_threshold_returns_none(tmp_path: Path) -> None:
    store = BrokerStore(tmp_path).init()
    now = datetime(2026, 8, 27, 20, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        _record_human_action(store, now - timedelta(minutes=i))
    # 5 actions < default threshold of 15
    assert reservoir_advisory(store, now=now) is None


def test_count_threshold_triggers_advisory(tmp_path: Path) -> None:
    store = BrokerStore(tmp_path).init()
    now = datetime(2026, 8, 27, 20, 0, 0, tzinfo=timezone.utc)
    for i in range(16):
        _record_human_action(store, now - timedelta(minutes=i))

    adv = reservoir_advisory(store, now=now, count_threshold=15)
    assert adv is not None
    assert "[reservoir advisory]" in adv
    assert "16 queue action(s) cleared" in adv
    assert "threshold: 15 actions" in adv
    assert "review quality" not in adv
    assert "Declared precaution" in adv


def test_span_threshold_triggers_advisory(tmp_path: Path) -> None:
    store = BrokerStore(tmp_path).init()
    now = datetime(2026, 8, 27, 21, 0, 0, tzinfo=timezone.utc)
    # 4 actions spread over 50 minutes (gap < 20 min between each)
    _record_human_action(store, now - timedelta(minutes=50))
    _record_human_action(store, now - timedelta(minutes=35))
    _record_human_action(store, now - timedelta(minutes=20))
    _record_human_action(store, now - timedelta(minutes=5))

    adv = reservoir_advisory(
        store,
        now=now,
        count_threshold=20,
        span_minutes_threshold=45,
        idle_gap_minutes=20,
    )
    assert adv is not None
    assert "[reservoir advisory]" in adv
    assert "4 queue action(s) cleared in 45m" in adv


def test_idle_gap_splits_sittings(tmp_path: Path) -> None:
    store = BrokerStore(tmp_path).init()
    now = datetime(2026, 8, 27, 22, 0, 0, tzinfo=timezone.utc)

    # Old sitting: 20 actions 2 hours ago
    for i in range(20):
        _record_human_action(store, now - timedelta(minutes=120 + i))

    # New sitting: only 2 actions just now
    _record_human_action(store, now - timedelta(minutes=5))
    _record_human_action(store, now - timedelta(minutes=1))

    # Current sitting has only 2 actions -> should not trigger 15-count threshold
    adv = reservoir_advisory(store, now=now, count_threshold=15, idle_gap_minutes=20)
    assert adv is None


def test_agent_actions_ignored(tmp_path: Path) -> None:
    store = BrokerStore(tmp_path).init()
    now = datetime(2026, 8, 27, 20, 0, 0, tzinfo=timezone.utc)

    # 30 agent actions
    for i in range(30):
        _record_agent_action(store, now - timedelta(minutes=i), agent="Builder")

    # Only 2 human actions
    _record_human_action(store, now - timedelta(minutes=2))
    _record_human_action(store, now - timedelta(minutes=1))

    assert reservoir_advisory(store, now=now, count_threshold=5) is None


def test_read_only_invariant(tmp_path: Path) -> None:
    """W1 invariant: reservoir_advisory must never invoke mutating methods."""
    store = BrokerStore(tmp_path).init()
    now = datetime(2026, 8, 27, 20, 0, 0, tzinfo=timezone.utc)
    for i in range(16):
        _record_human_action(store, now - timedelta(minutes=i))

    # Count audit rows before
    with store._connect() as conn:
        count_before = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]

    # Run advisory
    adv = reservoir_advisory(store, now=now)
    assert adv is not None

    # Count audit rows after
    with store._connect() as conn:
        count_after = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]

    assert count_before == count_after
