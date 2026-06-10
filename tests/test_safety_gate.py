from __future__ import annotations

import subprocess

import pytest

from orac.agents import build_core_agents
from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.llm import RulesBrain
from orac.models import CapabilityRequest, CapabilityStatus, Task, TaskStatus
from orac.policy import SAFETY_CRITICAL_PATHS, safety_critical_paths_touched


# --- the pure classification predicate (store-free, design §8.7) -----------


def test_write_to_governor_file_is_flagged() -> None:
    touched = safety_critical_paths_touched(
        "repo.write_file", {"path": "src/orac/policy.py", "content": "x = 1"}
    )
    assert touched == ["src/orac/policy.py"]


def test_absolute_path_to_governor_file_is_flagged() -> None:
    touched = safety_critical_paths_touched(
        "repo.write_file",
        {"path": "D:/Code/ORAC/src/orac/broker.py", "content": "x = 1"},
    )
    assert touched == ["D:/Code/ORAC/src/orac/broker.py"]


def test_windows_backslash_path_is_flagged() -> None:
    touched = safety_critical_paths_touched(
        "repo.write_file",
        {"path": r"D:\Code\ORAC\src\orac\council.py", "content": "x = 1"},
    )
    assert touched == [r"D:\Code\ORAC\src\orac\council.py"]


def test_grant_seed_is_safety_critical() -> None:
    assert "src/orac/prompts/agents.json" in SAFETY_CRITICAL_PATHS
    touched = safety_critical_paths_touched(
        "repo.write_file", {"path": "src/orac/prompts/agents.json", "content": "{}"}
    )
    assert touched


def test_commit_touching_governor_among_paths_is_flagged() -> None:
    touched = safety_critical_paths_touched(
        "git.commit",
        {"paths": ["src/orac/work.py", "src/orac/scrum.py"], "message": "edit"},
    )
    assert touched == ["src/orac/scrum.py"]


def test_lookalike_path_is_not_flagged() -> None:
    # A path that only resembles a governor file must not match (boundary check).
    assert safety_critical_paths_touched(
        "repo.write_file", {"path": "src/orac/notpolicy.py", "content": "x"}
    ) == []
    assert safety_critical_paths_touched(
        "repo.write_file", {"path": "vendor/src/orac/policy.py.bak", "content": "x"}
    ) == []


def test_ordinary_code_file_is_not_flagged() -> None:
    assert safety_critical_paths_touched(
        "repo.write_file", {"path": "src/orac/work.py", "content": "x"}
    ) == []


def test_read_of_governor_file_is_not_gated() -> None:
    # Only mutations are gated; reading or searching the governor is fine.
    assert safety_critical_paths_touched(
        "repo.read_file", {"path": "src/orac/policy.py"}
    ) == []
    assert safety_critical_paths_touched(
        "repo.search", {"query": "policy"}
    ) == []


def test_missing_or_empty_args_are_not_flagged() -> None:
    assert safety_critical_paths_touched("repo.write_file", None) == []
    assert safety_critical_paths_touched("repo.write_file", {}) == []
    assert safety_critical_paths_touched("git.commit", {"message": "no paths"}) == []


# --- the council Sentinel lens, end to end through the broker ---------------


def _repo(tmp_path) -> BrokerStore:
    (tmp_path / ".orac").mkdir()
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    return BrokerStore(tmp_path).init()


def test_sentinel_escalates_write_to_governor_file(tmp_path) -> None:
    store = _repo(tmp_path)
    store.grant("Builder", "repo.write_file")
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(title="harden the broker", status=TaskStatus.IN_PROGRESS)
    # A relative path the agent might name; resolved under the repo root, it is
    # the real broker.py — and must not be written without human approval.
    req = CapabilityRequest(
        agent="Builder",
        tool="repo.write_file",
        task_id=task.id,
        args={"path": "src/orac/broker.py", "content": "# tampered\n"},
    )

    result = broker.request(req, task)

    assert result.status is CapabilityStatus.PENDING
    assert "safety-critical" in result.message
    reviews = store.list_reviews(task.id)
    assert any(r["lens"] == "Sentinel" and r["decision"] == "escalate" for r in reviews)
    # the file was NOT written — escalation parks before dispatch
    assert not (tmp_path / "src" / "orac" / "broker.py").exists()


def test_sentinel_clears_after_human_approval(tmp_path) -> None:
    store = _repo(tmp_path)
    store.grant("Builder", "repo.write_file")
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(title="harden the council", status=TaskStatus.IN_PROGRESS)
    req = CapabilityRequest(
        agent="Builder",
        tool="repo.write_file",
        task_id=task.id,
        args={"path": "src/orac/council.py", "content": "# approved change\n"},
    )

    parked = broker.request(req, task)
    assert parked.status is CapabilityStatus.PENDING

    store.resolve_pending(parked.data["pending_id"], "approved")

    cleared = broker.request(req, task)
    assert cleared.status is CapabilityStatus.ALLOWED
    assert (tmp_path / "src" / "orac" / "council.py").read_text(
        encoding="utf-8"
    ) == "# approved change\n"


def test_sentinel_does_not_gate_ordinary_code_writes(tmp_path) -> None:
    store = _repo(tmp_path)
    store.grant("Builder", "repo.write_file")
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(title="ordinary feature", status=TaskStatus.IN_PROGRESS)
    req = CapabilityRequest(
        agent="Builder",
        tool="repo.write_file",
        task_id=task.id,
        args={"path": "src/orac/newfeature.py", "content": "def f(): return 1\n"},
    )

    result = broker.request(req, task)

    assert result.status is CapabilityStatus.ALLOWED
    # no Sentinel escalation recorded for ordinary work
    assert not any(r["lens"] == "Sentinel" for r in store.list_reviews(task.id))


def test_sentinel_gate_holds_against_builder_agent(tmp_path) -> None:
    # The gate must bind the Builder specifically — it is the only writer, so a
    # gate that exempted it would be no gate at all (design §8.7 / §4.6).
    store = _repo(tmp_path)
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    agent = next(a for a in build_core_agents(RulesBrain(), broker) if a.name == "Builder")
    task = Task(title="rewrite the policy", status=TaskStatus.IN_PROGRESS)

    agent._act = lambda t: bool(
        agent._use(t, "repo.write_file", path="src/orac/policy.py", content="# nope\n")
    )
    agent.work(task)

    assert task.status == TaskStatus.PENDING_APPROVAL
    assert any(
        r["lens"] == "Sentinel" and r["decision"] == "escalate"
        for r in store.list_reviews(task.id)
    )
