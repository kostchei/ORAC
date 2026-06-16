from __future__ import annotations

from orac.broker_store import BrokerStore
from orac.metrics import compute_metrics, render_metrics
from orac.models import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    CouncilVerdict,
    LensDecision,
    LensVerdict,
)


def _req(tool: str, task_id: str = "t1") -> CapabilityRequest:
    return CapabilityRequest(agent="Builder", tool=tool, task_id=task_id, args={})


def _result(status: CapabilityStatus, tool: str) -> CapabilityResult:
    return CapabilityResult(status=status, tool=tool, message="m", data={})


def test_compute_metrics_rolls_up_audit_reviews_and_queue(tmp_path) -> None:
    (tmp_path / ".orac").mkdir()
    store = BrokerStore(tmp_path).init()

    store.record_audit(_req("repo.write_file"), _result(CapabilityStatus.ALLOWED, "repo.write_file"))
    store.record_audit(_req("repo.write_file"), _result(CapabilityStatus.ALLOWED, "repo.write_file"))
    store.record_audit(_req("repo.run_tests"), _result(CapabilityStatus.ALLOWED, "repo.run_tests"))
    store.record_audit(_req("status_reporter"), _result(CapabilityStatus.DENIED, "status_reporter"))

    store.record_review(
        _req("return_review"),
        CouncilVerdict(
            status=CapabilityStatus.DENIED,
            lenses=(
                LensVerdict(lens="Security", decision=LensDecision.BLOCK, reason="secret leak"),
                LensVerdict(lens="Intent", decision=LensDecision.ESCALATE, reason="off goal"),
            ),
            reason="security floor",
        ),
    )

    m = compute_metrics(store)

    assert m["audit"]["total"] == 4
    assert m["audit"]["by_status"] == {"allowed": 3, "denied": 1}
    assert m["audit"]["by_tool"]["repo.write_file"] == 2
    assert m["reviews"]["total"] == 2  # two lens rows persisted
    assert m["reviews"]["by_lens"]["Security"] == {"block": 1}
    assert m["reviews"]["by_lens"]["Intent"] == {"escalate": 1}
    assert m["queue"]["pending_approvals"] == 0
    # render must not raise and should name the dimensions
    text = render_metrics(m)
    assert "audit" in text and "Security" in text


def test_metrics_empty_store_is_zeros_not_an_error(tmp_path) -> None:
    (tmp_path / ".orac").mkdir()
    store = BrokerStore(tmp_path).init()

    m = compute_metrics(store)

    assert m["audit"]["total"] == 0
    assert m["reviews"]["total"] == 0
    assert m["queue"] == {"pending_approvals": 0, "unacked_notifications": 0}
    assert "0 brokered call" in render_metrics(m)
