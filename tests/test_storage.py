from __future__ import annotations

import json
from pathlib import Path
import pytest
from orac.cli import main
from orac.models import Board, Task
from orac.storage import BoardStore, CorruptStateError, StaleBoardError, _BoardLock


def test_board_store_init_and_lifecycle(tmp_path) -> None:
    store = BoardStore(tmp_path)
    # init creates board if not exist
    board = store.init()
    assert isinstance(board, Board)
    assert store.board_path.exists()
    
    # Save a modified board
    board.created_at = "2026-06-12T00:00:00+00:00"
    store.save(board)
    assert store.backup_path.exists()
    
    # Load and verify
    loaded = store.load()
    assert loaded.created_at == "2026-06-12T00:00:00+00:00"


def test_load_and_save_json(tmp_path) -> None:
    store = BoardStore(tmp_path)
    custom_path = tmp_path / "custom.json"
    
    # load_json returns default when not existing
    default_data = {"key": "val"}
    data = store.load_json(custom_path, default_data)
    assert data == default_data
    
    # save_json writes the file
    store.save_json(custom_path, {"hello": "world"})
    assert custom_path.exists()
    
    loaded = store.load_json(custom_path, {})
    assert loaded == {"hello": "world"}


def test_atomic_save_leaves_original_on_failure(tmp_path, monkeypatch) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    
    # Write some initial content
    board.created_at = "original"
    store.save(board)
    original_content = store.board_path.read_text(encoding="utf-8")
    
    # Make os.replace fail to simulate error mid-atomic-save
    import os
    def mock_replace(src, dst):
        raise OSError("Simulated replacement failure")
    monkeypatch.setattr(os, "replace", mock_replace)
    
    # Attempting to save should raise OSError
    board.created_at = "modified"
    with pytest.raises(OSError, match="Simulated replacement failure"):
        store.save(board)
        
    # The original file must remain intact and unmodified
    assert store.board_path.exists()
    assert store.board_path.read_text(encoding="utf-8") == original_content
    
    # Check that any generated temporary files were cleaned up
    temp_files = list(tmp_path.glob("*.tmp"))
    assert not temp_files


def test_atomic_save_json_leaves_original_on_failure(tmp_path, monkeypatch) -> None:
    store = BoardStore(tmp_path)
    custom_path = tmp_path / "custom.json"
    
    store.save_json(custom_path, {"original": True})
    original_content = custom_path.read_text(encoding="utf-8")
    
    # Make os.replace fail
    import os
    def mock_replace(src, dst):
        raise OSError("Simulated replacement failure")
    monkeypatch.setattr(os, "replace", mock_replace)
    
    with pytest.raises(OSError, match="Simulated replacement failure"):
        store.save_json(custom_path, {"modified": True})
        
    assert custom_path.exists()
    assert custom_path.read_text(encoding="utf-8") == original_content
    
    # Check that no temp file is left behind
    temp_files = list(tmp_path.glob("*.tmp"))
    assert not temp_files


def test_load_corrupt_or_missing_board_raises(tmp_path) -> None:
    store = BoardStore(tmp_path)
    
    # Missing raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        store.load()
        
    # Corrupt raises json.JSONDecodeError
    (tmp_path / ".orac").mkdir(parents=True, exist_ok=True)
    store.board_path.write_text("invalid json", encoding="utf-8")
    with pytest.raises(CorruptStateError):
        store.load()


def test_recover_restores_last_good_backup(tmp_path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    board.created_at = "last-good"
    store.save(board)

    store.board_path.write_text("invalid json", encoding="utf-8")

    recovered = store.recover()
    assert recovered.created_at == "last-good"
    assert store.load().created_at == "last-good"


def test_concurrent_load_modify_save_raises_stale(tmp_path) -> None:
    store = BoardStore(tmp_path)
    store.init()
    # Two writers (daemon tick, UI server) load the same revision.
    a = store.load()
    b = store.load()
    b.add_task(Task(title="from-b"))
    store.save(b)
    a.add_task(Task(title="from-a"))
    # a's save would silently destroy b's task; it must raise instead.
    with pytest.raises(StaleBoardError):
        store.save(a)
    assert [task.title for task in store.load().tasks] == ["from-b"]


def test_sequential_saves_of_same_board_keep_working(tmp_path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    for n in range(3):
        board.add_task(Task(title=f"task-{n}"))
        store.save(board)
    loaded = store.load()
    assert len(loaded.tasks) == 3
    assert loaded.revision == 4  # init save + three task saves


def test_blind_overwrite_of_existing_board_raises_stale(tmp_path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    board.add_task(Task(title="keep"))
    store.save(board)
    # A Board that was never loaded from disk may not clobber existing state.
    with pytest.raises(StaleBoardError):
        store.save(Board())
    assert [task.title for task in store.load().tasks] == ["keep"]


def test_legacy_board_without_revision_migrates(tmp_path) -> None:
    store = BoardStore(tmp_path)
    store.state_dir.mkdir(parents=True, exist_ok=True)
    legacy = {"created_at": "x", "updated_at": "x", "tasks": []}
    store.board_path.write_text(json.dumps(legacy), encoding="utf-8")
    board = store.load()
    assert board.revision == 0
    store.save(board)
    assert store.load().revision == 1


def test_save_on_corrupt_board_fails_closed(tmp_path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    board.created_at = "good"
    store.save(board)
    store.board_path.write_text("invalid json", encoding="utf-8")
    # A corrupt board blocks saves until it is explicitly recovered.
    with pytest.raises(CorruptStateError):
        store.save(board)
    store.recover()
    assert store.load().created_at == "good"


def test_board_lock_blocks_second_holder(tmp_path) -> None:
    lock_path = tmp_path / ".orac" / "board.lock"
    with _BoardLock(lock_path):
        with pytest.raises(TimeoutError):
            with _BoardLock(lock_path, timeout_seconds=0.2):
                pass


def test_cli_board_recover(tmp_path, capsys) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    board.created_at = "good"
    store.save(board)
    store.board_path.write_text("invalid json", encoding="utf-8")
    assert main(["--root", str(tmp_path), "board", "recover"]) == 0
    assert "Restored" in capsys.readouterr().out
    assert store.load().created_at == "good"


def test_event_log_records_every_commit(tmp_path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()                       # revision 1 (empty)
    board.add_task(Task(title="first"))
    store.save(board)                          # revision 2 (one task)
    board.add_task(Task(title="second"))
    store.save(board)                          # revision 3 (two tasks)

    events = store.read_events()
    assert [e["revision"] for e in events] == [1, 2, 3]
    assert [e["tasks"] for e in events] == [0, 1, 2]
    # the change summary names what moved in the last commit
    assert len(events[-1]["changes"]["added"]) == 1


def test_rebuild_from_events_after_board_and_backup_lost(tmp_path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    board.add_task(Task(title="survive me"))
    store.save(board)
    rev = board.revision

    # Lose BOTH the current board and the last-good backup — only the log remains.
    store.board_path.unlink()
    store.backup_path.unlink()

    rebuilt = store.restore_from_events()
    assert store.board_path.exists()
    assert rebuilt.revision == rev
    assert [t.title for t in rebuilt.tasks] == ["survive me"]
    assert [t.title for t in store.load().tasks] == ["survive me"]


def test_read_events_skips_torn_final_line(tmp_path) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    board.add_task(Task(title="committed"))
    store.save(board)
    # Simulate a crash mid-append: a partial JSON line at the end of the log.
    with open(store.events_path, "a", encoding="utf-8") as f:
        f.write('{"seq": 99, "board": {"tasks":')   # truncated, no newline/close

    events = store.read_events()
    assert events and max(e["revision"] for e in events) == board.revision
    # the torn line is ignored, and rebuild still works
    assert store.rebuild_from_events().revision == board.revision


def test_board_events_and_rebuild_cli(tmp_path, capsys) -> None:
    store = BoardStore(tmp_path)
    board = store.init()
    board.add_task(Task(title="cli task"))
    store.save(board)

    assert main(["--root", str(tmp_path), "board", "events"]) == 0
    assert "board event" in capsys.readouterr().out.lower()

    store.board_path.unlink()
    store.backup_path.unlink()
    assert main(["--root", str(tmp_path), "board", "rebuild"]) == 0
    assert "Rebuilt" in capsys.readouterr().out
    assert [t.title for t in store.load().tasks] == ["cli task"]
