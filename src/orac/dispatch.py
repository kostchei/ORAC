from __future__ import annotations

from dataclasses import dataclass

from orac.broker_store import MAX_SUBAGENTS, BrokerStore

# (e) The both-must-agree DISPATCH gate.
#
# Spawning a subagent is a two-party decision, asymmetric by design:
#   - Orchestrator = proposer. It decided the decomposition; proposing IS its
#     agreement (and the plan already passed plan-review).
#   - Optimise = allocator. It must confirm there is room: a free roster slot AND
#     space in the resource band for this slice. Confirming IS its agreement.
#
# Both must agree, or no spawn. The roster cap is the hard admission floor
# (enforced again in admit_subagent); the band is the concurrency throttle — the
# 60% utilisation idea made concrete as "sum of active slices may not exceed the
# band". A refused spawn is not an error: the slice stays open and is retried when
# a slot frees, which is how the system stays within its resource share.

# The band ceiling: total resource slice across *active* subagents may not exceed
# this. With the default per-subagent slice of 0.25, a ceiling of 1.0 admits four
# concurrent doers before new spawns defer.
ACTIVE_SLICE_CEILING = 1.0

_EPSILON = 1e-9


@dataclass(frozen=True)
class DispatchDecision:
    agreed: bool
    reason: str


def optimise_admits(
    store: BrokerStore,
    resource_slice: float,
    *,
    band: float = ACTIVE_SLICE_CEILING,
    cap: int = MAX_SUBAGENTS,
) -> DispatchDecision:
    """Optimise's half: is there a roster slot and band room for this slice?"""
    if store.subagent_free_slots(cap) <= 0:
        return DispatchDecision(False, f"roster full ({cap}); no free slot")
    projected = store.active_slice_total() + resource_slice
    if projected > band + _EPSILON:
        return DispatchDecision(
            False,
            f"resource band full: active {store.active_slice_total():.2f} + "
            f"{resource_slice:.2f} would exceed band {band:.2f}",
        )
    return DispatchDecision(True, "slot and band available")


def both_agree(
    store: BrokerStore,
    orchestrator_proposed: bool,
    resource_slice: float,
    *,
    band: float = ACTIVE_SLICE_CEILING,
    cap: int = MAX_SUBAGENTS,
) -> DispatchDecision:
    """The spawn fires only if BOTH parties agree.

    Orchestrator agreement is carried in ``orchestrator_proposed`` (a slice from
    an approved plan); Optimise agreement is the resource check. Either party
    declining defers the spawn.
    """
    if not orchestrator_proposed:
        return DispatchDecision(False, "Orchestrator did not propose this spawn")
    return optimise_admits(store, resource_slice, band=band, cap=cap)
