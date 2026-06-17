from __future__ import annotations

from orac.broker_store import BrokerStore
from orac.models import Board, Task, TaskStatus
from orac.self_tune import (
    DECOMPOSE_THRESHOLD_DEFAULT,
    DECOMPOSE_THRESHOLD_KEY,
    THRESHOLD_MAX,
    THRESHOLD_MIN,
    goal_outcomes,
    maybe_self_tune,
    propose_threshold,
)


def _goal(status: TaskStatus) -> Task:
    return Task(title="g", work_kind="code", status=status, metadata={"goal": "do x"})


def _board(done: int, blocked: int, *, extra: list[Task] | None = None) -> Board:
    board = Board()
    for _ in range(done):
        board.add_task(_goal(TaskStatus.DONE))
    for _ in range(blocked):
        board.add_task(_goal(TaskStatus.BLOCKED))
    for task in extra or []:
        board.add_task(task)
    return board


# --- goal_outcomes: only terminal goal tasks count ----------------------------


def test_goal_outcomes_counts_only_terminal_goals() -> None:
    board = _board(
        3, 2,
        extra=[
            _goal(TaskStatus.IN_PROGRESS),          # in-flight goal: ignored
            Task(title="bookkeeping", status=TaskStatus.DONE),  # not a goal: ignored
        ],
    )
    assert goal_outcomes(board) == (3, 2)


# --- propose_threshold: pure, bounded control law -----------------------------


def test_hold_when_too_few_samples() -> None:
    assert propose_threshold(1, done=1, blocked=1) is None  # 2 < MIN_SAMPLES


def test_raise_when_blocked_rate_high() -> None:
    new, reason = propose_threshold(1, done=1, blocked=5)  # ~83% blocked
    assert new == 2
    assert "less eagerly" in reason


def test_lower_when_blocked_rate_low() -> None:
    new, reason = propose_threshold(3, done=19, blocked=1)  # 5% blocked
    assert new == 2
    assert "baseline" in reason


def test_dead_band_holds() -> None:
    assert propose_threshold(1, done=7, blocked=3) is None  # 30%: between low/high


def test_clamp_at_max_does_not_raise_further() -> None:
    assert propose_threshold(THRESHOLD_MAX, done=0, blocked=8) is None


def test_clamp_at_min_does_not_lower_below_baseline() -> None:
    assert propose_threshold(THRESHOLD_MIN, done=10, blocked=0) is None


# --- maybe_self_tune: apply within bounds, persist, notify, cooldown ----------


def test_apply_persists_threshold_and_notifies(tmp_path) -> None:
    store = BrokerStore(tmp_path).init()
    board = _board(0, 6)  # 100% blocked -> back off

    adj = maybe_self_tune(store, board, cooldown_seconds=0)

    assert adj is not None
    assert (adj.old, adj.new) == (DECOMPOSE_THRESHOLD_DEFAULT, DECOMPOSE_THRESHOLD_DEFAULT + 1)
    # persisted so scrum reads the new value
    assert store.get_tunable(DECOMPOSE_THRESHOLD_KEY, "1") == str(DECOMPOSE_THRESHOLD_DEFAULT + 1)
    # surfaced for review-after
    notes = store.list_notifications(unacked_only=True)
    assert any(n.tool == "config.decompose_threshold" for n in notes)


def test_cooldown_blocks_a_second_change(tmp_path) -> None:
    store = BrokerStore(tmp_path).init()
    board = _board(0, 6)

    first = maybe_self_tune(store, board, cooldown_seconds=0)
    assert first is not None
    # a real cooldown is now active -> the next call holds despite the same signal
    second = maybe_self_tune(store, board, cooldown_seconds=3600)
    assert second is None
    assert store.get_tunable(DECOMPOSE_THRESHOLD_KEY, "1") == str(first.new)


def test_hold_makes_no_change(tmp_path) -> None:
    store = BrokerStore(tmp_path).init()
    board = _board(7, 3)  # dead band
    assert maybe_self_tune(store, board, cooldown_seconds=0) is None
    assert store.get_tunable(DECOMPOSE_THRESHOLD_KEY, "unset") == "unset"
