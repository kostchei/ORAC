from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from orac.broker_store import BrokerStore
from orac.browser_brain import ensure_browser_foundation_ready
from orac.llm import build_brain, drain_foundation_spend_usd
from orac.notify import review_queue_summary
from orac.model_policy import (
    ModelPolicyStore,
    cooldown_browser_provider,
    ensure_lmstudio_model_loaded,
    verify_model_slots,
)
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
    policy_store = ModelPolicyStore(store)
    policy = policy_store.load_policy()

    # Ready the local model, then verify every configured slot names a model LM
    # Studio can actually load. A misconfigured slot is a fault to surface at
    # startup, not one to discover mid-build (no-fallback: throw, don't limp on).
    load = ensure_lmstudio_model_loaded(policy)
    print(f"LM Studio startup: {load.get('action')} — {load.get('message', '')}")
    slots = verify_model_slots(policy_store)
    print(f"Model slots: {slots['message']}")
    if slots["missing"]:
        raise RuntimeError(
            f"Configured model(s) not loadable in LM Studio: {slots['missing']}. "
            f"Available: {slots['available']}. Fix with `orac models set` or load the model."
        )

    if policy.get("browser_foundation_provider"):
        result = ensure_browser_foundation_ready(policy, orac_root=root)
        print(f"Browser foundation: {result.get('action')} — {result.get('message', '')}")
    print(f"ORAC daemon running every {interval_seconds}s. Press Ctrl+C to stop.")
    broker_store = BrokerStore(root).init()
    while True:
        tick = run_daemon_tick(store, cycles=cycles)
        print(
            f"tick brain={tick.brain} model={tick.model} touched={tick.touched_tasks} "
            f"done={tick.done_tasks} reason={tick.reason}"
        )
        # Notify transport (P6): surface the review queue each tick so an
        # unattended run reaches the operator instead of waiting to be polled.
        summary = review_queue_summary(broker_store)
        if not summary.is_clear:
            print(summary.message())
        time.sleep(interval_seconds)


def run_daemon_tick(store: BoardStore, cycles: int = 1) -> DaemonTick:
    policy_store = ModelPolicyStore(store)
    decision = policy_store.decide()
    board = store.load()
    try:
        result = Scrum(
            build_brain(decision.brain, model=decision.model),
            root=store.root,
            originate_when_idle=True,
            route_models=True,
            llm_lenses=True,
        ).run(board, cycles=cycles)
    except Exception as exc:
        _record_browser_provider_cooldown(policy_store, exc)
        raise
    store.save(board)
    # Record MEASURED foundation spend (from real API token usage), not a flat
    # estimate. Drains 0 when the tick used only local/browser brains (both free).
    spent = drain_foundation_spend_usd()
    if spent > 0:
        policy_store.record_foundation_spend(spent)
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


def _record_browser_provider_cooldown(
    policy_store: ModelPolicyStore, exc: Exception
) -> None:
    from orac.browser_brain import ProviderRateLimited  # noqa: PLC0415

    if not isinstance(exc, ProviderRateLimited):
        return
    provider = getattr(exc, "provider", "")
    if not provider:
        return
    cooldown_browser_provider(policy_store, str(provider), seconds=3600, reason=str(exc))
