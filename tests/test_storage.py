from __future__ import annotations

import json
from pathlib import Path
import pytest
from orac.models import Board
from orac.storage import BoardStore, CorruptStateError


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
