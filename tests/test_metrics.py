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


def test_metrics_gap_e_scored_and_repairs(tmp_path) -> None:
    from orac.broker_store import BrokerStore
    from orac.metrics import compute_metrics, render_metrics
    from orac.models import (
        CapabilityRequest,
        CapabilityStatus,
        CouncilVerdict,
        LensDecision,
        LensVerdict,
        Task,
        TaskStatus,
        WorkLog,
    )
    from orac.storage import BoardStore
    
    (tmp_path / ".orac").mkdir()
    store = BrokerStore(tmp_path).init()
    board_store = BoardStore(tmp_path)
    board = board_store.init()

    # 1. Record review with scores
    store.record_review(
        CapabilityRequest(agent="Council", tool="return_review", task_id="t1", args={}),
        CouncilVerdict(
            status=CapabilityStatus.DENIED,
            lenses=(
                LensVerdict(lens="Security", decision=LensDecision.BLOCK, reason="secret leak", score=2),
                LensVerdict(lens="Intent", decision=LensDecision.PASS, reason="fits goal", score=8),
                LensVerdict(lens="Simple", decision=LensDecision.PASS, reason="neat", score=9),
                LensVerdict(lens="Efficiency", decision=LensDecision.PASS, reason="fast", score=10),
            ),
            reason="security floor",
        ),
    )

    # 2. Record second round for task t1 with manual SQL inserts to control created_at
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO reviews (created_at, agent, tool, task_id, lens, decision, reason, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2030-06-16T12:00:00Z", "Council", "return_review", "t1", "Intent", "pass", "r1", 8)
        )
        conn.execute(
            "INSERT INTO reviews (created_at, agent, tool, task_id, lens, decision, reason, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2030-06-16T12:00:00Z", "Council", "return_review", "t1", "Simple", "pass", "r1", 8)
        )
        conn.execute(
            "INSERT INTO reviews (created_at, agent, tool, task_id, lens, decision, reason, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2030-06-16T12:00:00Z", "Council", "return_review", "t1", "Security", "pass", "r1", 8)
        )
        conn.execute(
            "INSERT INTO reviews (created_at, agent, tool, task_id, lens, decision, reason, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2030-06-16T12:00:00Z", "Council", "return_review", "t1", "Efficiency", "pass", "r1", 8)
        )

        conn.execute(
            "INSERT INTO reviews (created_at, agent, tool, task_id, lens, decision, reason, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2030-06-16T12:05:00Z", "Council", "return_review", "t1", "Intent", "pass", "r2", 9)
        )
        conn.execute(
            "INSERT INTO reviews (created_at, agent, tool, task_id, lens, decision, reason, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2030-06-16T12:05:00Z", "Council", "return_review", "t1", "Simple", "pass", "r2", 9)
        )
        conn.execute(
            "INSERT INTO reviews (created_at, agent, tool, task_id, lens, decision, reason, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2030-06-16T12:05:00Z", "Council", "return_review", "t1", "Security", "pass", "r2", 9)
        )
        conn.execute(
            "INSERT INTO reviews (created_at, agent, tool, task_id, lens, decision, reason, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2030-06-16T12:05:00Z", "Council", "return_review", "t1", "Efficiency", "pass", "r2", 9)
        )

    # 3. Create task board tasks to test rounds-to-done, repetition trips, and verification failures.
    # Primary goal task
    goal_task = Task(id="t1", title="implement feature", work_kind="code", status=TaskStatus.DONE)
    # Spawns a repair descendant
    repair_task = Task(id="r1", title="[code] repair: fix bug", parent_id="t1", status=TaskStatus.DONE)
    repair_task.metadata = {"contract": {"goal": "A prior attempt failed verification. Fix EXACTLY this failure"}}
    
    # Logs on task
    goal_task.add_log("Builder", "Session done after 4 step(s): success")
    goal_task.add_log("system", "Blocked: subtask did not pass verification (test failed)")
    
    # Repetition limit log
    repair_task.add_log("Builder", "Session blocked after 3 step(s): Repetition limit (3): repo.search called identically")

    board.add_task(goal_task)
    board.add_task(repair_task)
    board_store.save(board)

    m = compute_metrics(store)
    
    # Assert rounds to done
    assert m["rounds_to_done"]["t1"] == 1
    # Assert first vs final scores
    assert m["first_round_scores"]["t1"] == 7.15
    assert m["final_round_scores"]["t1"] == 9.0

    # Assert repetition trips
    assert m["tool_repetition_trips"] == 1

    # Assert verification failure rate
    assert m["verification_failure_rate"] == 1.0

    text = render_metrics(m)
    assert "rounds-to-done:" in text
    assert "task t1: 1 round(s)" in text
    assert "RETURN scores (first vs final):" in text
    assert "task t1: first=7.15, final=9.0" in text
    assert "tool repetition trips: 1" in text
    assert "verification-failure rate: 100.00%" in text
