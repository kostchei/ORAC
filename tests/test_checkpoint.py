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
