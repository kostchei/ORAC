"""Validate ORAC's load-bearing governance path end to end.

This is an operator smoke suite, not a replacement for pytest. It exercises real
broker dispatch calls against temporary ORAC stores/repos and proves the safety
claims that matter before widening ORAC beyond the code-writing bootstrap:

- clean dispatch allows and audits
- Intent blocks closed-task drift
- Efficiency blocks duplicate writes
- Optimise escalates over the fair-share band
- Sentinel escalates safety-critical self-modification before dispatch
- git.push runs review-after and records a notification
- standing grants clear approval only within their daily cap

Run from the repo root:

    python scripts/validate_governance_path.py
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orac.broker import ToolBroker  # noqa: E402
from orac.broker_store import BrokerStore  # noqa: E402
from orac.council import Council  # noqa: E402
from orac.models import CapabilityRequest, CapabilityStatus, Task, TaskStatus  # noqa: E402
from orac.policy import ApprovalMode  # noqa: E402


@dataclass(frozen=True)
class Check:
    name: str
    detail: str


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _init_repo(root: Path) -> BrokerStore:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".orac").mkdir()
    (root / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", ".gitignore")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    return BrokerStore(root).init()


def _init_repo_with_remote(root: Path) -> BrokerStore:
    repo = root / "repo"
    remote = root / "remote.git"
    store = _init_repo(repo)
    _git(root, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return store


def _req(tool: str, task: Task, **args: object) -> CapabilityRequest:
    return CapabilityRequest(agent="Builder", tool=tool, task_id=task.id, args=dict(args))


def _broker(store: BrokerStore) -> ToolBroker:
    return ToolBroker.from_store(store, repo_root=store.root)


def check_clean_dispatch(root: Path) -> Check:
    store = _init_repo(root / "clean-dispatch")
    broker = _broker(store)
    task = Task(title="normal work", status=TaskStatus.IN_PROGRESS)

    result = broker.request(_req("git.status", task, root=str(store.root)), task)

    assert result.status is CapabilityStatus.ALLOWED
    assert store.audit_log()[0].tool == "git.status"
    assert store.list_reviews() == []
    return Check("clean dispatch", "git.status allowed, audited, and all-pass reviews stayed quiet")


def check_intent_blocks_closed_task(root: Path) -> Check:
    store = _init_repo(root / "intent-block")
    broker = _broker(store)
    task = Task(title="already done", status=TaskStatus.DONE)

    result = broker.request(_req("git.status", task, root=str(store.root)), task)

    assert result.status is CapabilityStatus.DENIED
    assert any(
        r["lens"] == "Intent" and r["decision"] == "block"
        for r in store.list_reviews(task.id)
    )
    return Check("Intent block", "closed-task drift was denied and recorded by the Intent lens")


def check_efficiency_blocks_duplicate_write(root: Path) -> Check:
    store = _init_repo(root / "duplicate-write")
    broker = _broker(store)
    task = Task(title="write once", status=TaskStatus.IN_PROGRESS)
    args = {"path": "x.py", "content": "X = 1\n"}

    first = broker.request(_req("repo.write_file", task, **args), task)
    second = broker.request(_req("repo.write_file", task, **args), task)

    assert first.status is CapabilityStatus.ALLOWED
    assert second.status is CapabilityStatus.DENIED
    assert any(
        r["lens"] == "Efficiency" and r["decision"] == "block"
        for r in store.list_reviews(task.id)
    )
    return Check("Efficiency block", "identical repeated repo.write_file was denied")


def check_optimise_fair_share_escalates(root: Path) -> Check:
    store = _init_repo(root / "fair-share")
    broker = _broker(store)
    broker.council = Council(store=store, daily_rate_cap=1)
    task = Task(title="busy", status=TaskStatus.IN_PROGRESS)

    first = broker.request(_req("git.status", task, root=str(store.root)), task)
    second = broker.request(_req("git.status", task, root=str(store.root)), task)

    assert first.status is CapabilityStatus.ALLOWED
    assert second.status is CapabilityStatus.PENDING
    assert store.list_pending()
    assert any(
        r["lens"] == "Optimise" and r["decision"] == "escalate"
        for r in store.list_reviews(task.id)
    )
    return Check("Optimise escalation", "fair-share cap parked the second dispatch")


def check_sentinel_escalates_before_dispatch(root: Path) -> Check:
    store = _init_repo(root / "sentinel")
    broker = _broker(store)
    task = Task(title="touch governor", status=TaskStatus.IN_PROGRESS)

    result = broker.request(
        _req("repo.write_file", task, path="src/orac/policy.py", content="# unsafe\n"),
        task,
    )

    assert result.status is CapabilityStatus.PENDING
    assert not (store.root / "src" / "orac" / "policy.py").exists()
    assert any(
        r["lens"] == "Sentinel" and r["decision"] == "escalate"
        for r in store.list_reviews(task.id)
    )
    return Check("Sentinel escalation", "safety-critical write parked before file dispatch")


def check_git_push_notifies(root: Path) -> Check:
    store = _init_repo_with_remote(root / "notify")
    repo = store.root
    broker = _broker(store)
    task = Task(title="push review-after", status=TaskStatus.IN_PROGRESS)
    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "add mod")
    sha = _git(repo, "rev-parse", "HEAD")

    result = broker.request(_req("git.push", task, root=str(repo)), task)

    assert result.status is CapabilityStatus.ALLOWED
    (note,) = store.list_notifications()
    assert note.tool == "git.push"
    assert note.data["sha"] == sha
    return Check("review-after notify", "git.push dispatched and queued a rollback-capable notification")


def check_standing_grant_cap(root: Path) -> Check:
    # No physical/communications tool is live yet, so force the risk gate to
    # APPROVE for this smoke only. This validates the real standing-grant branch:
    # within cap it dispatches + notifies; over cap it parks for a human.
    import orac.broker as broker_module

    store = _init_repo(root / "standing-grant")
    store.create_standing_grant("Builder", "git.status", daily_cap=1, reason="one check")
    broker = _broker(store)
    broker.council = None
    task = Task(title="standing grant", status=TaskStatus.IN_PROGRESS)
    req = _req("git.status", task, root=str(store.root))

    original: Callable = broker_module.approval_mode_for
    broker_module.approval_mode_for = lambda tool, args=None: ApprovalMode.APPROVE
    try:
        first = broker.request(req, task)
        second = broker.request(req, task)
    finally:
        broker_module.approval_mode_for = original

    assert first.status is CapabilityStatus.ALLOWED
    assert second.status is CapabilityStatus.PENDING
    assert len(store.list_notifications()) == 1
    assert len(store.list_pending()) == 1
    return Check("standing-grant cap", "grant allowed one APPROVE-gated call, then parked over cap")


def check_physical_dispatch_and_standing_grant(root: Path) -> Check:
    from orac.physical_store import PhysicalStore

    store = _init_repo(root / "physical-dispatch")
    pstore = PhysicalStore(store.root)
    broker = _broker(store)
    task = Task(id="t-phys-1", title="operate devices", status=TaskStatus.IN_PROGRESS)

    # 1. Read state is AUTO -> allowed immediately
    read_req = CapabilityRequest(
        agent="Operator",
        tool="physical.read_state",
        task_id=task.id,
        args={"device_id": "living_room_light"},
    )
    read_res = broker.request(read_req, task)
    assert read_res.status is CapabilityStatus.ALLOWED

    # 2. Prepare action is NOTIFY -> allowed immediately
    prep_req = CapabilityRequest(
        agent="Operator",
        tool="physical.prepare_action",
        task_id=task.id,
        args={"device_id": "living_room_light", "service": "turn_on"},
    )
    prep_res = broker.request(prep_req, task)
    assert prep_res.status is CapabilityStatus.ALLOWED
    action_id = prep_res.data["id"]

    # 3. Execute action without standing grant -> parked as PENDING
    exec_req = CapabilityRequest(
        agent="Operator",
        tool="physical.execute_action",
        task_id=task.id,
        args={"action_id": action_id},
    )
    exec_res = broker.request(exec_req, task)
    assert exec_res.status is CapabilityStatus.PENDING
    assert len(store.list_pending()) == 1

    # 4. Create standing grant with daily_cap=1 for Operator on execute_action
    store.create_standing_grant(
        "Operator", "physical.execute_action", daily_cap=1, reason="daily light automation"
    )

    # Create fresh prepared action for the standing grant run
    pstore.record_device_action("living_room_light", action_time="2020-01-01T00:00:00+00:00")
    prep2 = pstore.create_prepared_action(
        task_id=task.id, device_id="living_room_light", service="turn_on"
    )
    exec_req2 = CapabilityRequest(
        agent="Operator",
        tool="physical.execute_action",
        task_id=task.id,
        args={"action_id": prep2.id},
    )
    exec_res2 = broker.request(exec_req2, task)
    assert exec_res2.status is CapabilityStatus.ALLOWED
    assert any(n.tool == "physical.execute_action" for n in store.list_notifications())

    # 5. Second execute call exceeds daily cap -> parks as PENDING
    pstore.record_device_action("living_room_light", action_time="2020-01-01T00:00:00+00:00")
    prep3 = pstore.create_prepared_action(
        task_id=task.id, device_id="living_room_light", service="turn_off"
    )
    exec_req3 = CapabilityRequest(
        agent="Operator",
        tool="physical.execute_action",
        task_id=task.id,
        args={"action_id": prep3.id},
    )
    exec_res3 = broker.request(exec_req3, task)
    assert exec_res3.status is CapabilityStatus.PENDING

    return Check(
        "physical dispatch & standing grant",
        "read_state AUTO, prepare_action NOTIFY, execute_action APPROVE parked then cleared via standing grant",
    )


CHECKS: tuple[Callable[[Path], Check], ...] = (
    check_clean_dispatch,
    check_intent_blocks_closed_task,
    check_efficiency_blocks_duplicate_write,
    check_optimise_fair_share_escalates,
    check_sentinel_escalates_before_dispatch,
    check_git_push_notifies,
    check_standing_grant_cap,
    check_physical_dispatch_and_standing_grant,
)


def run_validation(root: Path | None = None) -> list[Check]:
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
        return [check(root) for check in CHECKS]
    td = Path(tempfile.mkdtemp(prefix="orac-governance-"))
    try:
        return [check(td) for check in CHECKS]
    finally:
        # On Windows, SQLite/WAL files can remain locked very briefly after the
        # last connection closes. Validation should not fail after all checks
        # pass just because temp cleanup needs another moment.
        shutil.rmtree(td, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Optional directory for temporary validation repos; kept after the run.",
    )
    args = parser.parse_args(argv)

    try:
        checks = run_validation(args.workdir)
    except Exception as exc:  # noqa: BLE001 - smoke script should report a single clear failure
        print(f"FAIL governance validation: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for check in checks:
        print(f"PASS {check.name}: {check.detail}")
    print(f"\nGovernance path validation passed ({len(checks)} checks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
