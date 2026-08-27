"""Short supervised soak validation: run a few real daemon ticks against the
live LM Studio models and report what each tick did, including the review queue.

Not a test fixture — a throwaway operator script to confirm the
originate -> build -> verify -> queue chain works end to end before an unattended
endurance run. Safe to delete.
"""
from __future__ import annotations

import sys
import time
import subprocess
from dataclasses import asdict
from typing import Any

from orac.broker_store import BrokerStore
from orac.daemon import run_daemon_tick
from orac.model_policy import (
    ModelPolicyStore,
    ensure_lmstudio_model_loaded,
    verify_model_slots,
)
from orac.notify import review_queue_summary
from orac.storage import BoardStore

ROOT = "."

_BAD_LOG_MARKERS = (
    "step budget exhausted",
    "could not produce a usable verdict",
    "malformed structured",
    "invalid structured",
)


def _active_ids(store: BrokerStore) -> set[int]:
    return {
        item.id
        for status in ("proposed", "active")
        for item in store.list_subagents(status=status)
    }


def _log_offsets(store: BoardStore) -> dict[str, int]:
    return {task.id: len(task.work_log) for task in store.load().tasks}


def _new_bad_logs(store: BoardStore, offsets: dict[str, int]) -> list[str]:
    problems: list[str] = []
    for task in store.load().tasks:
        for entry in task.work_log[offsets.get(task.id, 0) :]:
            lowered = entry.message.lower()
            if any(marker in lowered for marker in _BAD_LOG_MARKERS):
                problems.append(f"{task.id}/{entry.agent}: {entry.message}")
    return problems


def _git_state(root: str) -> dict[str, str]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    return {
        "branch": run("branch", "--show-current"),
        "head": run("rev-parse", "HEAD"),
        "status": run("status", "--porcelain"),
    }


def main(ticks: int | None = None) -> None:
    ticks = ticks if ticks is not None else (int(sys.argv[1]) if len(sys.argv) > 1 else 3)
    baseline_git = _git_state(ROOT)
    if baseline_git["status"]:
        raise SystemExit(
            "Canary requires a clean Git worktree so model changes can be attributed "
            f"to the observed run. Dirty paths:\n{baseline_git['status']}"
        )
    store = BoardStore(ROOT)
    store.init()
    policy_store = ModelPolicyStore(store)
    policy = policy_store.load_policy()

    load = ensure_lmstudio_model_loaded(policy)
    print(f"[startup] LM Studio: {load.get('action')} — {load.get('message', '')}")
    slots = verify_model_slots(policy_store)
    print(f"[startup] slots: {slots['message']} | missing={slots['missing']}")
    if slots["missing"]:
        raise SystemExit(f"slots missing: {slots['missing']}")

    broker_store = BrokerStore(ROOT).init()
    baseline_notifications = {item.id for item in broker_store.list_notifications()}
    baseline_pending = {item.id for item in broker_store.list_pending()}
    baseline_active = _active_ids(broker_store)
    baseline_done = {task.id for task in store.load().tasks if task.status.value == "done"}
    log_offsets = _log_offsets(store)
    print(
        "[baseline] "
        f"review_notifications={sorted(baseline_notifications)} "
        f"pending={sorted(baseline_pending)} active_subagents={sorted(baseline_active)}"
    )

    failures: list[str] = []
    observations: list[dict[str, Any]] = []
    for i in range(1, ticks + 1):
        t0 = time.monotonic()
        try:
            tick = run_daemon_tick(store, cycles=1)
        except Exception as exc:  # live canary: report a bounded failure, no traceback
            failures.append(f"tick {i} raised {type(exc).__name__}: {exc}")
            print(f"[tick {i}/{ticks}] ERROR {type(exc).__name__}: {exc}")
            break
        dt = time.monotonic() - t0
        observations.append({"tick": i, "seconds": round(dt, 1), **asdict(tick)})
        print(
            f"[tick {i}/{ticks} {dt:5.1f}s] brain={tick.brain} model={tick.model} "
            f"touched={tick.touched_tasks} done={tick.done_tasks} reason={tick.reason}"
        )
        summary = review_queue_summary(broker_store)
        print(f"[tick {i}] review queue: {summary.message()}")

    final_notifications = {item.id for item in broker_store.list_notifications()}
    final_pending = {item.id for item in broker_store.list_pending()}
    final_active = _active_ids(broker_store)
    final_git = _git_state(ROOT)
    final_done = {task.id for task in store.load().tasks if task.status.value == "done"}
    new_notifications = sorted(final_notifications - baseline_notifications)
    new_pending = sorted(final_pending - baseline_pending)
    leaked_active = sorted(final_active - baseline_active)
    newly_done = sorted(final_done - baseline_done)
    bad_logs = _new_bad_logs(store, log_offsets)
    if new_notifications:
        failures.append(f"unexpected new review notification(s): {new_notifications}")
    if new_pending:
        failures.append(f"unexpected new pending approval(s): {new_pending}")
    if leaked_active:
        failures.append(f"leaked active subagent reservation(s): {leaked_active}")
    if not newly_done:
        failures.append("no task reached done during the observed ticks")
    if final_git["status"]:
        failures.append(f"worktree left dirty:\n{final_git['status']}")
    failures.extend(f"bad work log: {item}" for item in bad_logs)

    board = store.load()
    print("\n[board] task summary:")
    for task in board.tasks:
        origin = task.metadata.get("origin", "-")
        print(f"  {task.id[:8]} {task.status.value:14} kind={task.work_kind} "
              f"origin={origin} :: {task.title[:60]}")

    print(f"\n[observations] {observations}")
    print(
        "[git] "
        f"branch={baseline_git['branch']}->{final_git['branch']} "
        f"head={baseline_git['head'][:8]}->{final_git['head'][:8]} "
        f"newly_done={newly_done}"
    )
    if failures:
        print("[result] FAIL")
        for failure in failures:
            print(f"  - {failure}")
        raise SystemExit(1)
    print(
        "[result] PASS — completed verified work with no new queue entries, "
        "malformed verdicts, budget exhaustion, dirty files, or leaked reservations"
    )


if __name__ == "__main__":
    main()
