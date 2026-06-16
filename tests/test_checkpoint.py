from __future__ import annotations

import subprocess

from orac.code_adapters import CodeAdapterSet
from orac.models import CapabilityRequest


def _git_repo(tmp_path):
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    return CodeAdapterSet((tmp_path.resolve(),))


def _call(adapters: CodeAdapterSet, tool: str, **args):
    return adapters.adapters()[tool](
        CapabilityRequest(agent="Builder", tool=tool, task_id="t1", args=args)
    )


def test_restore_checkpoint_reverts_edits_and_removes_since_added_files(tmp_path) -> None:
    adapters = _git_repo(tmp_path)
    root = str(tmp_path)

    ckpt = _call(adapters, "repo.checkpoint", root=root, label="before edit")
    sha = ckpt.data["sha"]

    # Mutate a tracked file and add a brand-new untracked file after the checkpoint.
    (tmp_path / "mod.py").write_text("def add(a, b):\n    return a - b  # bug\n", encoding="utf-8")
    (tmp_path / "new_module.py").write_text("JUNK = 1\n", encoding="utf-8")

    result = _call(adapters, "repo.restore_checkpoint", root=root, sha=sha)

    # The edit is reverted and the since-added file is gone — the tree matches the
    # checkpoint exactly.
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert not (tmp_path / "new_module.py").exists()
    assert "new_module.py" in result.data["removed"]


def test_restore_checkpoint_restores_a_file_deleted_after_checkpoint(tmp_path) -> None:
    adapters = _git_repo(tmp_path)
    root = str(tmp_path)

    # A second tracked file exists at checkpoint time.
    (tmp_path / "keep.py").write_text("VALUE = 42\n", encoding="utf-8")
    ckpt = _call(adapters, "repo.checkpoint", root=root, label="with keep.py")
    sha = ckpt.data["sha"]

    (tmp_path / "keep.py").unlink()  # deleted after the checkpoint

    _call(adapters, "repo.restore_checkpoint", root=root, sha=sha)

    assert (tmp_path / "keep.py").read_text(encoding="utf-8") == "VALUE = 42\n"


def test_restore_checkpoint_rejects_a_non_checkpoint_sha(tmp_path) -> None:
    adapters = _git_repo(tmp_path)
    root = str(tmp_path)
    import pytest

    with pytest.raises(ValueError, match="not a checkpoint commit"):
        _call(adapters, "repo.restore_checkpoint", root=root, sha="deadbeef")


def test_checkpoint_does_not_touch_head_or_working_tree(tmp_path) -> None:
    adapters = _git_repo(tmp_path)
    root = str(tmp_path)
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()

    _call(adapters, "repo.checkpoint", root=root, label="noop")

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    assert head_before == head_after  # checkpoint left HEAD untouched
    # working tree unchanged (only the tracked file + gitignore present)
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"


def test_auto_checkpoint_on_first_write(tmp_path) -> None:
    from orac.broker_store import BrokerStore
    from orac.broker import ToolBroker
    from orac.models import Task, TaskStatus

    # Initialize a git repo with a commit
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )

    (tmp_path / ".orac").mkdir()
    store = BrokerStore(tmp_path).init()
    # Grant permissions so it doesn't get pending-gated
    store.grant("Builder", "repo.write_file")
    
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(id="task-abc", title="feature", status=TaskStatus.IN_PROGRESS)

    # First write should trigger auto-checkpoint
    req1 = CapabilityRequest(
        agent="Builder",
        tool="repo.write_file",
        task_id=task.id,
        args={"path": "mod.py", "content": "x = 2\n"},
    )
    broker.request(req1, task)

    # Verify checkpoint is recorded
    root_str = str(tmp_path.resolve())
    ckpt_sha = store.latest_checkpoint(task.id, root_str)
    assert ckpt_sha is not None

    # Subsequent write should NOT create a new checkpoint (should keep the original pre-mutation one)
    req2 = CapabilityRequest(
        agent="Builder",
        tool="repo.write_file",
        task_id=task.id,
        args={"path": "mod.py", "content": "x = 3\n"},
    )
    broker.request(req2, task)

    # The recorded checkpoint SHA should remain unchanged
    ckpt_sha2 = store.latest_checkpoint(task.id, root_str)
    assert ckpt_sha2 == ckpt_sha


def test_rollback_via_checkpoint(tmp_path) -> None:
    from orac.broker_store import BrokerStore
    from orac.broker import ToolBroker
    from orac.models import Task, TaskStatus, CapabilityResult, CapabilityStatus
    from orac.cli import cmd_rollback
    import argparse

    # Initialize a git repo with a commit
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )

    (tmp_path / ".orac").mkdir()
    store = BrokerStore(tmp_path).init()
    store.grant("Builder", "repo.write_file")
    
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(id="task-xyz", title="feature", status=TaskStatus.IN_PROGRESS)

    # First write triggers auto-checkpoint (capturing x = 1)
    req1 = CapabilityRequest(
        agent="Builder",
        tool="repo.write_file",
        task_id=task.id,
        args={"path": "mod.py", "content": "x = 2\n"},
    )
    res1 = broker.request(req1, task)
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "x = 2\n"

    # We record a notification without a sha or rollback contract, which represents a step that has no commit yet.
    # We want rollback to restore to the latest checkpoint.
    # Create notification for res1
    note_id = store.record_notification(req1, CapabilityResult(status=CapabilityStatus.ALLOWED, tool="repo.write_file", message="Wrote file"))

    # Verify rollback
    # We construct a mock BoardStore because cli cmd_rollback takes a BoardStore (which has root)
    from orac.storage import BoardStore
    board = BoardStore(tmp_path)
    
    # Run CLI command rollback for the notification id
    args = argparse.Namespace(id=note_id, push=False)
    exit_code = cmd_rollback(board, args)
    assert exit_code == 0

    # Verify working tree is rolled back to checkpoint state (x = 1)
    assert (tmp_path / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert store.get_notification(note_id).acked
