from __future__ import annotations

from orac.broker_store import BrokerStore
from orac.entropy import collect_idle_garbage, scan_entropy
from orac.models import Board, CapabilityRequest, CapabilityResult, CapabilityStatus, Task


def test_scan_entropy_reports_observed_orphan_and_oversized_log(tmp_path) -> None:
    store = BrokerStore(tmp_path).init()
    board = Board(tasks=[Task(title="orphan", parent_id="missing")])
    log = tmp_path / ".orac" / "connector.log"
    log.write_text("123456", encoding="utf-8")

    findings = scan_entropy(board, store, tmp_path, log_size_threshold=5)

    by_key = {finding.key: finding for finding in findings}
    assert "orphaned-child-tasks" in by_key
    assert "Observed 1 child task" in by_key["orphaned-child-tasks"].observation
    assert "oversized-connector-logs" in by_key
    assert all(finding.acceptance_criteria for finding in findings)


def test_idle_gc_retires_expired_runtime_lease(tmp_path) -> None:
    store = BrokerStore(tmp_path).init()
    lease_id = store.admit_subagent("parent", "builder", "do x", "x")

    result = collect_idle_garbage(store, tmp_path, stale_subagent_seconds=-1)

    assert result.stale_subagents_reaped == 1
    assert store.list_subagents()[0].id == lease_id
    assert store.list_subagents()[0].status == "retired"


def test_unacked_review_is_operator_work_not_code_entropy(tmp_path) -> None:
    store = BrokerStore(tmp_path).init()
    store.record_notification(
        CapabilityRequest(agent="Builder", tool="git.push", task_id="t"),
        CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool="git.push",
            message="Pushed a reviewed change.",
        ),
    )

    findings = scan_entropy(Board(), store, tmp_path)

    assert "unacked-review-queue" not in {finding.key for finding in findings}
