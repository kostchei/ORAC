from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orac.broker_store import BrokerStore, Notification, PendingApproval
from orac.models import (
    Board,
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    Task,
    TaskStatus,
)
from orac.storage import BoardStore
from orac.ui_server import _reviews_payload, _state_payload


class DummyRequest:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self, length: int) -> bytes:
        return self.body[:length]


def test_reviews_payload_structure(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    store.init()
    bstore = BrokerStore(tmp_path).init()

    # Add a pending approval and an unacked notification
    pending_id = bstore.create_pending(
        CapabilityRequest(
            agent="Messenger",
            tool="channel.send",
            task_id="task-100",
            args={"target": "+12345", "text": "Hello"},
        )
    )
    note_id = bstore.record_notification(
        CapabilityRequest(
            agent="Builder",
            tool="git.push",
            task_id="task-101",
            args={"remote": "origin", "branch": "build/task-101"},
        ),
        CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool="git.push",
            message="pushed",
            data={"sha": "abcdef123456"},
        ),
    )

    payload = _reviews_payload(store)
    assert payload["summary"]["pending_approvals"] == 1
    assert payload["summary"]["unacked_notifications"] == 1
    assert len(payload["pending_approvals"]) == 1
    assert payload["pending_approvals"][0]["id"] == pending_id
    assert len(payload["notifications"]) == 1
    assert payload["notifications"][0]["id"] == note_id


def test_merge_conflict_surfacing_in_state_and_reviews(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    store.init()

    task = Task(id="conflict-task", title="Conflicted Task", status=TaskStatus.IN_PROGRESS)
    task.metadata["merge_conflict"] = {
        "task_id": "conflict-task",
        "resolved_at": "2026-08-28T00:00:00Z",
        "resolution": "newest_wins",
        "note": "Concurrent edits merged",
    }
    board = store.load()
    board.add_task(task)
    store.save(board)

    state = _state_payload(store)
    assert "merge_conflicts" in state
    assert len(state["merge_conflicts"]) == 1
    assert state["merge_conflicts"][0]["task_id"] == "conflict-task"

    reviews = _reviews_payload(store)
    assert "merge_conflicts" in reviews
    assert len(reviews["merge_conflicts"]) == 1
