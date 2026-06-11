"""Short supervised soak validation: run a few real daemon ticks against the
live LM Studio models and report what each tick did, including the review queue.

Not a test fixture — a throwaway operator script to confirm the
originate -> build -> verify -> queue chain works end to end before an unattended
endurance run. Safe to delete.
"""
from __future__ import annotations

import sys
import time

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
TICKS = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def main() -> None:
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
    for i in range(1, TICKS + 1):
        t0 = time.monotonic()
        tick = run_daemon_tick(store, cycles=1)
        dt = time.monotonic() - t0
        print(
            f"[tick {i}/{TICKS} {dt:5.1f}s] brain={tick.brain} model={tick.model} "
            f"touched={tick.touched_tasks} done={tick.done_tasks} reason={tick.reason}"
        )
        summary = review_queue_summary(broker_store)
        print(f"[tick {i}] review queue: {summary.message()}")

    board = store.load()
    print("\n[board] task summary:")
    for task in board.tasks:
        origin = task.metadata.get("origin", "-")
        print(f"  {task.id[:8]} {task.status.value:14} kind={task.work_kind} "
              f"origin={origin} :: {task.title[:60]}")


if __name__ == "__main__":
    main()
