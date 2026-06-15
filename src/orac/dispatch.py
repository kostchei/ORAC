from __future__ import annotations

from dataclasses import dataclass

from orac.broker_store import MAX_SUBAGENTS, BrokerStore

# (e) The both-must-agree DISPATCH gate.
#
# Spawning a subagent is a two-party decision:
#   - Orchestrator proposes the slice.
#   - Optimise confirms there is a free roster slot.
#
# The four council agents review and constrain edges. They are not the worker
# pool. Worker fan-out is bounded by MAX_SUBAGENTS, not by a hidden resource-band
# calculation that turns 0.25 slices into a four-worker ceiling.


@dataclass(frozen=True)
class DispatchDecision:
    agreed: bool
    reason: str


def optimise_admits(
    store: BrokerStore,
    *,
    cap: int = MAX_SUBAGENTS,
) -> DispatchDecision:
    """Optimise's half: is there a free roster slot for another subagent?"""
    store.reap_stale_subagents()
    if store.subagent_free_slots(cap) <= 0:
        return DispatchDecision(False, f"roster full ({cap}); no free slot")
    return DispatchDecision(True, "roster slot available")


def both_agree(
    store: BrokerStore,
    orchestrator_proposed: bool,
    *,
    cap: int = MAX_SUBAGENTS,
) -> DispatchDecision:
    """The spawn fires only if Orchestrator proposed it and Optimise admits it."""
    if not orchestrator_proposed:
        return DispatchDecision(False, "Orchestrator did not propose this spawn")
    return optimise_admits(store, cap=cap)
