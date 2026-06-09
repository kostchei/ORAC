from __future__ import annotations

import dataclasses

import pytest

from orac.models import (
    CapabilityRequest,
    CapabilityStatus,
    CouncilVerdict,
    EdgeKind,
    Externality,
    LensDecision,
    LensVerdict,
    Reversibility,
    ReviewContext,
    RiskClass,
    Task,
)


def test_enum_values_are_stable_strings() -> None:
    assert EdgeKind.DISPATCH == "dispatch"
    assert {e.value for e in EdgeKind} == {"dispatch", "tool_call", "tool_chain", "return"}
    assert {d.value for d in LensDecision} == {"pass", "block", "escalate"}
    assert {r.value for r in Reversibility} == {"reversible", "hard", "irreversible"}
    assert {x.value for x in Externality} == {
        "local",
        "external_private",
        "external_public",
        "financial",
        "physical",
    }


def test_review_context_composes_the_capability_contract() -> None:
    task = Task(title="demo")
    ctx = ReviewContext(
        edge=EdgeKind.TOOL_CALL,
        request=CapabilityRequest(agent="Builder", tool="repo.apply_patch", task_id=task.id),
        task=task,
        risk=RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL),
    )

    assert ctx.edge is EdgeKind.TOOL_CALL
    assert ctx.request.agent == "Builder"
    assert ctx.risk.externality is Externality.LOCAL


def test_council_verdict_carries_lens_verdicts_and_reuses_capability_status() -> None:
    verdict = CouncilVerdict(
        status=CapabilityStatus.PENDING,
        lenses=(
            LensVerdict(lens="Intent", decision=LensDecision.PASS, reason="on goal"),
            LensVerdict(lens="Optimise", decision=LensDecision.ESCALATE, reason="over band"),
        ),
        reason="Optimise escalated.",
    )

    assert verdict.status is CapabilityStatus.PENDING
    assert isinstance(verdict.lenses, tuple)
    assert [v.decision for v in verdict.lenses] == [LensDecision.PASS, LensDecision.ESCALATE]


def test_contracts_are_immutable() -> None:
    verdict = LensVerdict(lens="Simple", decision=LensDecision.BLOCK, reason="rebuild beats patch")
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.reason = "changed"  # type: ignore[misc]
