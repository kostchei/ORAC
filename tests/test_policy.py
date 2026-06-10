from __future__ import annotations

import itertools

import pytest

from orac.models import Externality, Reversibility, RiskClass
from orac.policy import ApprovalMode, approval_mode, approval_mode_for, risk_class


def test_risk_class_classifies_known_tools() -> None:
    assert risk_class("git.commit") == RiskClass(Reversibility.REVERSIBLE, Externality.LOCAL)
    assert risk_class("git.push") == RiskClass(Reversibility.HARD, Externality.EXTERNAL_PRIVATE)
    # a journaling tool
    assert risk_class("handoff_tracker").externality is Externality.LOCAL


def test_unclassified_tool_fails_closed() -> None:
    with pytest.raises(ValueError):
        risk_class("shell.run_anything")


def test_modes_match_the_user_policy_shape() -> None:
    # Review-after, not ask-before: code work never blocks the loop.
    assert approval_mode_for("fs_read") is ApprovalMode.AUTO
    assert approval_mode_for("repo.write_file") is ApprovalMode.AUTO
    assert approval_mode_for("git.commit") is ApprovalMode.AUTO
    assert approval_mode_for("git.revert") is ApprovalMode.AUTO
    # push runs unattended but lands in the review queue
    assert approval_mode_for("git.push") is ApprovalMode.NOTIFY


def test_approve_is_reserved_for_the_irreversible_external() -> None:
    # The blocking gate exists only where rollback does not: comms-grade
    # external-irreversible, financial, physical.
    assert approval_mode(
        RiskClass(Reversibility.IRREVERSIBLE, Externality.EXTERNAL_PUBLIC)
    ) is ApprovalMode.APPROVE
    assert approval_mode(
        RiskClass(Reversibility.REVERSIBLE, Externality.FINANCIAL)
    ) is ApprovalMode.APPROVE
    assert approval_mode(
        RiskClass(Reversibility.HARD, Externality.PHYSICAL)
    ) is ApprovalMode.APPROVE


def test_throttle_table_is_total() -> None:
    # every (reversibility x externality) pair resolves to a mode; no gaps.
    for rev, ext in itertools.product(Reversibility, Externality):
        assert isinstance(approval_mode(RiskClass(rev, ext)), ApprovalMode)
