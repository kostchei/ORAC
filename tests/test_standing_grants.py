from __future__ import annotations

import subprocess

import pytest

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.council import today_utc
from orac.models import CapabilityRequest, CapabilityStatus, Task, TaskStatus
from orac.policy import ApprovalMode


def _store(tmp_path) -> BrokerStore:
    (tmp_path / ".orac").mkdir()
    return BrokerStore(tmp_path).init()


def _repo_store(tmp_path) -> BrokerStore:
    (tmp_path / ".orac").mkdir()
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    return BrokerStore(tmp_path).init()


# --- store layer -----------------------------------------------------------


def test_standing_grant_create_and_match_any_args(tmp_path) -> None:
    store = _store(tmp_path)
    gid = store.create_standing_grant("Operator", "execute_action", 3, "feed the fish")

    grant = store.standing_grant_for("Operator", "execute_action", {"device": "feeder"})
    assert grant is not None and grant.id == gid
    assert grant.args_pattern is None  # unpinned: matches any args


def test_standing_grant_args_pinned_match_is_exact(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_standing_grant(
        "Operator", "execute_action", 1, "feed at 8am", args={"device": "feeder", "g": 5}
    )

    assert store.standing_grant_for("Operator", "execute_action", {"device": "feeder", "g": 5})
    # a different argument set is not covered by the pinned grant
    assert store.standing_grant_for("Operator", "execute_action", {"device": "feeder", "g": 9}) is None


def test_pinned_grant_is_preferred_over_unpinned(tmp_path) -> None:
    store = _store(tmp_path)
    unpinned = store.create_standing_grant("Operator", "execute_action", 9, "any")
    pinned = store.create_standing_grant(
        "Operator", "execute_action", 1, "exact", args={"device": "feeder"}
    )

    grant = store.standing_grant_for("Operator", "execute_action", {"device": "feeder"})
    assert grant is not None and grant.id == pinned
    assert grant.daily_cap == 1  # the narrow grant won, not the cap-9 unpinned one
    assert unpinned != pinned


def test_revoke_standing_grant(tmp_path) -> None:
    store = _store(tmp_path)
    gid = store.create_standing_grant("Operator", "execute_action", 3, "feed")

    store.revoke_standing_grant(gid)

    assert store.list_standing_grants() == []
    assert store.standing_grant_for("Operator", "execute_action", {}) is None
    assert len(store.list_standing_grants(active_only=False)) == 1


def test_revoke_unknown_standing_grant_raises(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(KeyError):
        store.revoke_standing_grant(999)


def test_zero_cap_is_not_a_grant(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.create_standing_grant("Operator", "execute_action", 0, "nonsense")


# --- broker integration ----------------------------------------------------


def _approve_everything(monkeypatch) -> None:
    # No tool classifies as APPROVE by default (code is review-after; comms/
    # physical have no doers yet). Pin the risk verdict to APPROVE so these tests
    # exercise the standing-grant path against the human-approval gate directly.
    monkeypatch.setattr(
        "orac.broker.approval_mode_for", lambda tool, args=None: ApprovalMode.APPROVE
    )


def test_standing_grant_clears_the_approve_park(tmp_path, monkeypatch) -> None:
    store = _repo_store(tmp_path)
    store.grant("Builder", "git.status")
    store.create_standing_grant("Builder", "git.status", daily_cap=5, reason="recurring check")
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    broker.council = None  # isolate the risk-APPROVE path from the council floor
    task = Task(title="recurring", status=TaskStatus.IN_PROGRESS)
    _approve_everything(monkeypatch)

    result = broker.request(
        CapabilityRequest(agent="Builder", tool="git.status", task_id=task.id,
                          args={"root": str(tmp_path)}),
        task,
    )

    assert result.status is CapabilityStatus.ALLOWED  # ran, did not park
    assert store.list_pending() == []
    # review-after: a pre-authorised action still lands in the notify queue
    assert len(store.list_notifications()) == 1


def test_standing_grant_falls_back_to_human_over_cap(tmp_path, monkeypatch) -> None:
    store = _repo_store(tmp_path)
    store.grant("Builder", "git.status")
    store.create_standing_grant("Builder", "git.status", daily_cap=1, reason="once a day")
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    broker.council = None
    task = Task(title="capped", status=TaskStatus.IN_PROGRESS)
    _approve_everything(monkeypatch)
    req = CapabilityRequest(agent="Builder", tool="git.status", task_id=task.id,
                            args={"root": str(tmp_path)})

    first = broker.request(req, task)
    assert first.status is CapabilityStatus.ALLOWED  # within cap

    second = broker.request(req, task)
    assert second.status is CapabilityStatus.PENDING  # over cap -> parks for a human
    assert len(store.list_pending()) == 1


def test_standing_grant_does_not_bypass_safety_gate(tmp_path, monkeypatch) -> None:
    # The crucial invariant: a standing grant pre-authorises the risk-APPROVE
    # park, but it must NEVER waive the council's Sentinel floor. The system
    # cannot grant itself permission to edit its own governor.
    store = _repo_store(tmp_path)
    store.grant("Builder", "repo.write_file")
    store.create_standing_grant("Builder", "repo.write_file", daily_cap=99, reason="broad")
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(title="tamper", status=TaskStatus.IN_PROGRESS)
    _approve_everything(monkeypatch)  # even with APPROVE + a broad standing grant...

    result = broker.request(
        CapabilityRequest(
            agent="Builder", tool="repo.write_file", task_id=task.id,
            args={"path": "src/orac/policy.py", "content": "# nope\n"},
        ),
        task,
    )

    # ...the Sentinel lens still escalates the self-modification to a human.
    assert result.status is CapabilityStatus.PENDING
    assert any(
        r["lens"] == "Sentinel" and r["decision"] == "escalate"
        for r in store.list_reviews(task.id)
    )
    assert not (tmp_path / "src" / "orac" / "policy.py").exists()
