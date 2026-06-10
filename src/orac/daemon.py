from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from orac.browser_brain import ensure_browser_foundation_ready
from orac.llm import build_brain
from orac.model_policy import ModelPolicyStore
from orac.scrum import Scrum
from orac.storage import BoardStore


@dataclass(frozen=True)
class DaemonTick:
    cycles: int
    touched_tasks: int
    done_tasks: int
    brain: str
    model: str
    reason: str


def run_daemon(root: Path | str = ".", interval_seconds: int = 60, cycles: int = 1) -> None:
    store = BoardStore(root)
    store.init()
    policy = ModelPolicyStore(store).load_policy()
    if policy.get("browser_foundation_provider"):
        result = ensure_browser_foundation_ready(policy, orac_root=root)
        print(f"Browser foundation: {result.get('action')} — {result.get('message', '')}")
    print(f"ORAC daemon running every {interval_seconds}s. Press Ctrl+C to stop.")
    while True:
        tick = run_daemon_tick(store, cycles=cycles)
        print(
            f"tick brain={tick.brain} model={tick.model} touched={tick.touched_tasks} "
            f"done={tick.done_tasks} reason={tick.reason}"
        )
        time.sleep(interval_seconds)


def run_daemon_tick(store: BoardStore, cycles: int = 1) -> DaemonTick:
    policy_store = ModelPolicyStore(store)
    decision = policy_store.decide()
    board = store.load()
    result = Scrum(
        build_brain(decision.brain, model=decision.model),
        root=store.root,
        originate_when_idle=True,
        route_models=True,
        llm_lenses=True,
    ).run(board, cycles=cycles)
    store.save(board)
    if decision.brain == "foundation" and result.touched_tasks:
        policy_store.record_foundation_spend(policy_store.estimated_cycle_spend())
    return DaemonTick(
        cycles=result.cycles,
        touched_tasks=result.touched_tasks,
        done_tasks=result.done_tasks,
        brain=decision.brain,
        model=decision.model,
        reason=decision.reason,
    )


def tick_payload(store: BoardStore, cycles: int = 1) -> dict[str, object]:
    return asdict(run_daemon_tick(store, cycles=cycles))
