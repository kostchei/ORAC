from __future__ import annotations

from dataclasses import dataclass

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.models import Board, Task, TaskStatus
from orac.shared_lessons import (
    MAX_LESSONS_INJECTED,
    record_slice_outcome,
    render_for_contract,
)
from orac.work import run_goal_task


def _store(tmp_path) -> BrokerStore:
    return BrokerStore(tmp_path).init()


# --- store: record / scope isolation / recency cap ----------------------------


def test_lessons_are_scoped_to_their_goal(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_lesson("goalA", "result", "A worked")
    store.record_lesson("goalB", "failure", "B failed")
    a = store.lessons_for("goalA")
    assert [l.text for l in a] == ["A worked"]
    assert store.lessons_for("goalB")[0].kind == "failure"


def test_lessons_for_returns_recent_capped_oldest_first(tmp_path) -> None:
    store = _store(tmp_path)
    for i in range(MAX_LESSONS_INJECTED + 5):
        store.record_lesson("g", "result", f"note {i}")
    got = store.lessons_for("g", limit=MAX_LESSONS_INJECTED)
    assert len(got) == MAX_LESSONS_INJECTED
    # most-recent kept, but presented oldest-first
    assert got[0].text == "note 5"
    assert got[-1].text == f"note {MAX_LESSONS_INJECTED + 4}"


# --- record_slice_outcome: only verified terminal slices ----------------------


def test_records_done_slice_as_ok(tmp_path) -> None:
    store = _store(tmp_path)
    child = Task(title="[code] add endpoint", status=TaskStatus.DONE)
    child.add_log("Builder", "tests passed")
    record_slice_outcome(store, "g", child)
    lessons = store.lessons_for("g")
    assert lessons[0].kind == "result"
    assert "add endpoint" in lessons[0].text and "tests passed" in lessons[0].text


def test_records_blocked_slice_as_failure(tmp_path) -> None:
    store = _store(tmp_path)
    child = Task(title="[code] migrate db", status=TaskStatus.BLOCKED)
    child.add_log("system", "did not pass verification: schema drift")
    record_slice_outcome(store, "g", child)
    assert store.lessons_for("g")[0].kind == "failure"


def test_parked_slice_records_nothing(tmp_path) -> None:
    store = _store(tmp_path)
    child = Task(title="[comms] notify", status=TaskStatus.PENDING_APPROVAL)
    record_slice_outcome(store, "g", child)
    assert store.lessons_for("g") == []


# --- render_for_contract ------------------------------------------------------


def test_render_is_empty_without_lessons(tmp_path) -> None:
    assert render_for_contract(_store(tmp_path), "g") == ""


def test_render_marks_ok_and_avoid(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_lesson("g", "result", "did X")
    store.record_lesson("g", "failure", "do not Y")
    block = render_for_contract(store, "g")
    assert "[OK] did X" in block
    assert "[AVOID] do not Y" in block


# --- end-to-end: lessons reach the doer's contract (read-before) --------------


@dataclass
class _Result:
    status: str
    summary: str


class _RecordingSession:
    """Stands in for AgentSession: captures the contract the doer is handed."""

    def __init__(self, *a, **k) -> None:
        self.contracts: list[str] = []

    def run(self, task, contract):
        self.contracts.append(contract)
        return _Result(status="done", summary="ok")


def test_lessons_are_injected_into_the_doer_contract(tmp_path, monkeypatch) -> None:
    (tmp_path / ".orac").mkdir()
    broker = ToolBroker.from_store(BrokerStore(tmp_path).init())
    board = Board()
    parent = Task(title="parent", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    session = _RecordingSession()
    import orac.work as work_mod

    monkeypatch.setattr(work_mod, "verify_goal_done", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(work_mod, "AgentSession", lambda *a, **k: session)

    run_goal_task(
        board, parent, goal="g", acceptance_criteria=("works",),
        work_kind="code", brain=None, broker=broker,
        context={
            "repo_root": str(tmp_path),
            "shared_lessons": "- [AVOID] do not use the legacy API",
        },
    )

    contract = session.contracts[0]
    assert "SHARED LESSONS" in contract
    assert "do not use the legacy API" in contract
    # injected as a dedicated block, not leaked as a raw context key:value line
    assert "shared_lessons:" not in contract
