from __future__ import annotations

import pytest

from orac.broker import ToolBroker
from orac.models import (
    CapabilityRequest,
    CapabilityStatus,
    Externality,
    Reversibility,
    Task,
)
from orac.policy import ApprovalMode, approval_mode_for, risk_class
from orac.skills_adapters import SKILL_WRITE_TOOLS

_FM = "---\nname: greet\ndescription: hi\n---\nbody\n"


def _task() -> Task:
    return Task(title="t")


def _broker(tmp_path) -> ToolBroker:
    return ToolBroker.from_manifests(repo_root=tmp_path)


# --- risk model: skills run unattended (auto) and are reversible-local --------


def test_skill_tools_classified_reversible_local() -> None:
    for tool in ("skill.list", "skill.view", *SKILL_WRITE_TOOLS):
        rc = risk_class(tool)
        assert rc.reversibility is Reversibility.REVERSIBLE
        assert rc.externality is Externality.LOCAL
        assert approval_mode_for(tool) is ApprovalMode.AUTO


# --- grant enforcement: the invariant PR #4 broke -----------------------------


def test_council_can_read_skills(tmp_path) -> None:
    res = _broker(tmp_path).request(
        CapabilityRequest(agent="Intent", tool="skill.list", task_id="t", args={}),
        _task(),
    )
    assert res.status is CapabilityStatus.ALLOWED


def test_council_cannot_write_skills(tmp_path) -> None:
    # The council plans and reviews but never writes — skill.create is denied
    # for a non-Builder agent at the grant edge, before any dispatch.
    res = _broker(tmp_path).request(
        CapabilityRequest(
            agent="Efficiency",
            tool="skill.create",
            task_id="t",
            args={"name": "x", "content": _FM},
        ),
        _task(),
    )
    assert res.status is CapabilityStatus.DENIED


def test_builder_can_write_and_archive_skills(tmp_path) -> None:
    broker = _broker(tmp_path)
    task = _task()
    created = broker.request(
        CapabilityRequest(
            agent="Builder",
            tool="skill.create",
            task_id="t",
            args={"name": "greet", "content": _FM},
        ),
        task,
    )
    assert created.status is CapabilityStatus.ALLOWED

    archived = broker.request(
        CapabilityRequest(
            agent="Builder", tool="skill.archive", task_id="t", args={"name": "greet"}
        ),
        task,
    )
    assert archived.status is CapabilityStatus.ALLOWED
    assert (tmp_path / ".orac" / "skills" / ".archive").is_dir()


def test_unknown_skill_tool_is_rejected(tmp_path) -> None:
    # There is deliberately no hard-delete tool; it is not in the catalog.
    res = _broker(tmp_path).request(
        CapabilityRequest(agent="Builder", tool="skill.delete", task_id="t", args={"name": "x"}),
        _task(),
    )
    assert res.status is CapabilityStatus.ERROR
