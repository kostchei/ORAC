from __future__ import annotations

import json
import sqlite3
import subprocess

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.cli import main
from orac.models import (
    CapabilityRequest,
    CapabilityStatus,
    CouncilVerdict,
    LensDecision,
    LensVerdict,
    Task,
    TaskStatus,
)


def _git(root, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def _init_repo_with_remote(tmp_path) -> BrokerStore:
    """A real repo with a bare 'origin' so git.push / rollback --push are live."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".orac").mkdir()
    (repo / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    _git(repo, "init", "-b", "main")
    _git(repo, "add", ".gitignore")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return BrokerStore(repo).init()


def _push_via_broker(store: BrokerStore, task: Task) -> None:
    store.grant("Builder", "git.push")
    broker = ToolBroker.from_store(store, repo_root=store.root)
    result = broker.request(
        CapabilityRequest(
            agent="Builder",
            tool="git.push",
            task_id=task.id,
            args={"root": str(store.root)},
        ),
        task,
    )
    assert result.status is CapabilityStatus.ALLOWED


def _commit_change(repo, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def test_push_notification_records_pushed_sha(tmp_path) -> None:
    store = _init_repo_with_remote(tmp_path)
    repo = store.root
    sha = _commit_change(repo, "mod.py", "x = 1\n")
    task = Task(title="push it", status=TaskStatus.IN_PROGRESS)

    _push_via_broker(store, task)

    (note,) = store.list_notifications()
    assert note.tool == "git.push"
    assert note.data["sha"] == sha
    assert note.data["branch"] == "main"
    assert sha[:8] in note.message


def test_reviews_lists_pending_notifications_and_verdicts(tmp_path, capsys) -> None:
    store = _init_repo_with_remote(tmp_path)
    repo = store.root
    _commit_change(repo, "mod.py", "x = 1\n")
    task = Task(title="push it", status=TaskStatus.IN_PROGRESS)
    _push_via_broker(store, task)

    # A parked request plus the lens verdict that caused it, as the broker
    # records them when a lens escalates.
    req = CapabilityRequest(agent="Builder", tool="git.push", task_id=task.id)
    pending_id = store.create_pending(req)
    store.record_review(
        req,
        CouncilVerdict(
            status=CapabilityStatus.PENDING,
            lenses=(
                LensVerdict(
                    lens="Optimise",
                    decision=LensDecision.ESCALATE,
                    reason="over the fair-share band",
                ),
            ),
            reason="over the fair-share band",
        ),
    )

    assert main(["--root", str(repo), "reviews"]) == 0
    out = capsys.readouterr().out
    assert f"[{pending_id}]" in out
    assert "Pending approvals" in out
    assert "Optimise[escalate]: over the fair-share band" in out
    assert "Completed actions awaiting review" in out
    assert "git.push" in out
    assert "Lens verdicts" in out


def test_reviews_json_export(tmp_path, capsys) -> None:
    store = _init_repo_with_remote(tmp_path)
    _commit_change(store.root, "mod.py", "x = 1\n")
    task = Task(title="push it", status=TaskStatus.IN_PROGRESS)
    _push_via_broker(store, task)

    assert main(["--root", str(store.root), "reviews", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending_approvals"] == []
    (note,) = payload["notifications"]
    assert note["tool"] == "git.push"
    assert note["data"]["branch"] == "main"
    assert payload["lens_verdicts"] == []


def test_reviews_clear_queue(tmp_path, capsys) -> None:
    (tmp_path / ".orac").mkdir()
    BrokerStore(tmp_path).init()

    assert main(["--root", str(tmp_path), "reviews"]) == 0
    assert "Review queue is clear." in capsys.readouterr().out


def test_ack_via_cli(tmp_path, capsys) -> None:
    store = _init_repo_with_remote(tmp_path)
    _commit_change(store.root, "mod.py", "x = 1\n")
    task = Task(title="push it", status=TaskStatus.IN_PROGRESS)
    _push_via_broker(store, task)
    (note,) = store.list_notifications()

    assert main(["--root", str(store.root), "ack", str(note.id)]) == 0
    assert "Acked" in capsys.readouterr().out
    assert store.list_notifications() == []
    assert store.get_notification(note.id).acked


def test_ack_unknown_id_fails(tmp_path, capsys) -> None:
    (tmp_path / ".orac").mkdir()
    BrokerStore(tmp_path).init()

    assert main(["--root", str(tmp_path), "ack", "999"]) == 1


def test_approve_and_deny_via_cli(tmp_path, capsys) -> None:
    (tmp_path / ".orac").mkdir()
    store = BrokerStore(tmp_path).init()
    req = CapabilityRequest(agent="Builder", tool="git.push", task_id="t1")
    approve_id = store.create_pending(req)
    deny_id = store.create_pending(
        CapabilityRequest(agent="Builder", tool="git.push", task_id="t2")
    )

    assert main(["--root", str(tmp_path), "approve", str(approve_id)]) == 0
    assert main(["--root", str(tmp_path), "deny", str(deny_id)]) == 0

    assert store.get_pending(approve_id).status == "approved"
    assert store.get_pending(deny_id).status == "denied"
    assert store.list_pending() == []


def test_deny_unknown_id_fails(tmp_path) -> None:
    (tmp_path / ".orac").mkdir()
    BrokerStore(tmp_path).init()

    assert main(["--root", str(tmp_path), "deny", "999"]) == 1


def test_rollback_reverts_pushed_commit_and_acks(tmp_path, capsys) -> None:
    store = _init_repo_with_remote(tmp_path)
    repo = store.root
    bad_sha = _commit_change(repo, "mod.py", "x = 1\n")
    task = Task(title="push it", status=TaskStatus.IN_PROGRESS)
    _push_via_broker(store, task)
    (note,) = store.list_notifications()

    assert main(["--root", str(repo), "rollback", str(note.id), "--push"]) == 0

    # The inverse commit removed the file, locally and on the remote.
    assert not (repo / "mod.py").exists()
    head = _git(repo, "rev-parse", "HEAD")
    assert head != bad_sha
    assert _git(tmp_path / "remote.git", "rev-parse", "main") == head
    # The reviewed notification is closed out.
    assert store.get_notification(note.id).acked
    # The human's revert and push are in the same audit log as agent actions.
    audited = {(entry.agent, entry.tool) for entry in store.audit_log()}
    assert ("human", "git.revert") in audited
    assert ("human", "git.push") in audited


def test_rollback_without_recorded_sha_fails_closed(tmp_path, capsys) -> None:
    (tmp_path / ".orac").mkdir()
    store = BrokerStore(tmp_path).init()
    from orac.models import CapabilityResult

    note_id = store.record_notification(
        CapabilityRequest(agent="Builder", tool="git.push", task_id="t1"),
        CapabilityResult(
            status=CapabilityStatus.ALLOWED, tool="git.push", message="Pushed."
        ),
    )

    assert main(["--root", str(tmp_path), "rollback", str(note_id)]) == 1
    assert "no recorded commit sha" in capsys.readouterr().out
    assert not store.get_notification(note_id).acked


def test_migration_adds_data_json_to_existing_db(tmp_path) -> None:
    # A database created before the data_json column existed must be upgraded
    # in place by init(), keeping its rows readable.
    state_dir = tmp_path / ".orac"
    state_dir.mkdir()
    conn = sqlite3.connect(state_dir / "broker.db")
    conn.executescript(
        """
        CREATE TABLE notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            agent       TEXT NOT NULL,
            tool        TEXT NOT NULL,
            task_id     TEXT NOT NULL,
            message     TEXT NOT NULL,
            args_json   TEXT NOT NULL,
            acked       INTEGER NOT NULL DEFAULT 0,
            acked_at    TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO notifications "
        "(created_at, agent, tool, task_id, message, args_json) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'Builder', 'git.push', 't1', 'Pushed.', '{}')"
    )
    conn.commit()
    conn.close()

    store = BrokerStore(tmp_path).init()

    (note,) = store.list_notifications()
    assert note.data == {}
    assert note.message == "Pushed."
