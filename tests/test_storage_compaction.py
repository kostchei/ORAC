from __future__ import annotations

import json
from pathlib import Path
import pytest

from orac.models import Board, Task
from orac.storage import BoardStore


def test_compact_events_basic(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()

    # Create 10 saves to generate 10 events
    for i in range(10):
        board.add_task(Task(title=f"Task {i}"))
        store.save(board)

    events_before = store.read_events()
    assert len(events_before) == 11  # init + 10 saves

    # Compact keeping 4 latest events
    pruned = store.compact_events(keep=4, archive=True)
    assert pruned == 7

    events_after = store.read_events()
    assert len(events_after) == 4
    assert events_after == events_before[-4:]

    # Check archive
    assert store.events_archive_path.exists()
    archived_lines = store.events_archive_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(archived_lines) == 7
    archived_events = [json.loads(line) for line in archived_lines]
    assert archived_events == events_before[:7]

    # Rebuild from compacted events still works
    rebuilt = store.rebuild_from_events()
    assert len(rebuilt.tasks) == len(board.tasks)


def test_compact_events_noop_when_under_limit(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    store.save(board)

    pruned = store.compact_events(keep=10)
    assert pruned == 0
    assert len(store.read_events()) == 2


def test_compact_events_without_archive(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    for i in range(5):
        board.add_task(Task(title=f"Task {i}"))
        store.save(board)

    pruned = store.compact_events(keep=2, archive=False)
    assert pruned == 4
    assert not store.events_archive_path.exists()
    assert len(store.read_events()) == 2
