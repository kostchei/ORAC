from __future__ import annotations

import subprocess

import pytest

from orac.agent_registry import get_tool_map, load_agent_profiles
from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.code_adapters import WRITE_TOOLS, code_adapters_for
from orac.models import CapabilityRequest, CapabilityStatus, Task, TaskStatus
from orac.policy import safety_critical_paths_touched


def _adapter(tmp_path):
    return code_adapters_for((tmp_path,))["repo.edit_file"]


def _req(tmp_path, **args) -> CapabilityRequest:
    return CapabilityRequest(agent="Builder", tool="repo.edit_file", task_id="t1", args=args)


# --- adapter behaviour ------------------------------------------------------


def test_edit_replaces_unique_occurrence(tmp_path) -> None:
    f = tmp_path / "mod.py"
    f.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    result = _adapter(tmp_path)(_req(tmp_path, path=str(f), old="a - b", new="a + b"))

    assert result.name == "repo.edit_file"
    assert f.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"


def test_edit_missing_anchor_raises_and_writes_nothing(tmp_path) -> None:
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not found"):
        _adapter(tmp_path)(_req(tmp_path, path=str(f), old="y = 2", new="y = 3"))

    assert f.read_text(encoding="utf-8") == "x = 1\n"  # untouched


def test_edit_ambiguous_anchor_raises(tmp_path) -> None:
    f = tmp_path / "mod.py"
    f.write_text("v = 1\nv = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        _adapter(tmp_path)(_req(tmp_path, path=str(f), old="v = 1", new="v = 2"))

    assert f.read_text(encoding="utf-8") == "v = 1\nv = 1\n"  # untouched


def test_edit_identical_old_new_raises(tmp_path) -> None:
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="identical"):
        _adapter(tmp_path)(_req(tmp_path, path=str(f), old="x = 1", new="x = 1"))


def test_edit_outside_repo_root_is_refused(tmp_path) -> None:
    outside = tmp_path.parent / "escape.py"
    outside.write_text("secret\n", encoding="utf-8")

    with pytest.raises(PermissionError):
        _adapter(tmp_path)(_req(tmp_path, path=str(outside), old="secret", new="leaked"))


# --- governance integration -------------------------------------------------


def test_edit_file_is_a_write_tool_and_builder_only() -> None:
    assert "repo.edit_file" in WRITE_TOOLS
    profiles = {p.slug: p for p in load_agent_profiles()}
    assert "repo.edit_file" in profiles["builder"].tools
    for slug in ("intent", "optimiser", "simples", "efficiency", "orchestrator"):
        assert "repo.edit_file" not in profiles[slug].tools


def test_edit_file_is_in_the_tool_catalog() -> None:
    spec = get_tool_map()["repo.edit_file"]
    assert set(("path", "old", "new")) <= set(spec.inputs)


def test_sentinel_gate_covers_edit_file(tmp_path) -> None:
    # The new write tool must be gated by the safety floor exactly like
    # repo.write_file — an edit to the governor escalates to a human.
    assert safety_critical_paths_touched(
        "repo.edit_file", {"path": "src/orac/policy.py", "old": "a", "new": "b"}
    ) == ["src/orac/policy.py"]

    (tmp_path / ".orac").mkdir()
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    store = BrokerStore(tmp_path).init()
    store.grant("Builder", "repo.edit_file")
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(title="tamper via edit", status=TaskStatus.IN_PROGRESS)

    result = broker.request(
        CapabilityRequest(
            agent="Builder", tool="repo.edit_file", task_id=task.id,
            args={"path": "src/orac/broker.py", "old": "x", "new": "y"},
        ),
        task,
    )

    assert result.status is CapabilityStatus.PENDING
    assert any(
        r["lens"] == "Sentinel" and r["decision"] == "escalate"
        for r in store.list_reviews(task.id)
    )


def test_edit_file_through_broker_makes_a_surgical_change(tmp_path) -> None:
    (tmp_path / ".orac").mkdir()
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    store = BrokerStore(tmp_path).init()
    store.grant("Builder", "repo.edit_file")
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    target = tmp_path / "feature.py"
    target.write_text("VERSION = '1.0'\n", encoding="utf-8")
    task = Task(title="bump version", status=TaskStatus.IN_PROGRESS)

    result = broker.request(
        CapabilityRequest(
            agent="Builder", tool="repo.edit_file", task_id=task.id,
            args={"path": str(target), "old": "1.0", "new": "1.1"},
        ),
        task,
    )

    assert result.status is CapabilityStatus.ALLOWED
    assert target.read_text(encoding="utf-8") == "VERSION = '1.1'\n"
