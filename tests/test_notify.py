from __future__ import annotations

from orac.broker_store import BrokerStore
from orac.models import CapabilityRequest, CapabilityResult, CapabilityStatus
from orac.notify import review_queue_summary
from orac.ui_server import _reviews_payload, _state_payload
from orac.storage import BoardStore


def _store(tmp_path) -> BrokerStore:
    (tmp_path / ".orac").mkdir()
    return BrokerStore(tmp_path).init()


def _notify(store: BrokerStore, tool: str = "git.push") -> None:
    store.record_notification(
        CapabilityRequest(agent="Builder", tool=tool, task_id="t1"),
        CapabilityResult(status=CapabilityStatus.ALLOWED, tool=tool, message="did it"),
    )


def test_summary_is_clear_when_queue_empty(tmp_path) -> None:
    summary = review_queue_summary(_store(tmp_path))

    assert summary.is_clear
    assert summary.total == 0
    assert summary.message() == "Review queue clear."


def test_summary_counts_notifications_and_pending(tmp_path) -> None:
    store = _store(tmp_path)
    _notify(store)
    _notify(store)
    store.create_pending(CapabilityRequest(agent="Builder", tool="git.push", task_id="t2"))

    summary = review_queue_summary(store)

    assert summary.unacked_notifications == 2
    assert summary.pending_approvals == 1
    assert summary.total == 3
    assert not summary.is_clear
    msg = summary.message()
    assert "1 pending approval" in msg
    assert "2 action(s) awaiting review" in msg


def test_acked_notifications_drop_out_of_the_summary(tmp_path) -> None:
    store = _store(tmp_path)
    nid = store.record_notification(
        CapabilityRequest(agent="Builder", tool="git.push", task_id="t1"),
        CapabilityResult(status=CapabilityStatus.ALLOWED, tool="git.push", message="x"),
    )
    assert review_queue_summary(store).unacked_notifications == 1

    store.ack_notification(nid)

    assert review_queue_summary(store).is_clear


def test_state_payload_includes_review_queue(tmp_path) -> None:
    board_store = BoardStore(tmp_path)
    board_store.init()
    bstore = BrokerStore(tmp_path).init()
    _notify(bstore)

    payload = _state_payload(board_store)

    assert payload["review_queue"]["unacked_notifications"] == 1
    assert payload["review_queue"]["total"] == 1


def test_reviews_payload_mirrors_the_cli_queue(tmp_path) -> None:
    board_store = BoardStore(tmp_path)
    board_store.init()
    bstore = BrokerStore(tmp_path).init()
    _notify(bstore)
    bstore.create_pending(CapabilityRequest(agent="Builder", tool="git.push", task_id="t2"))
    bstore.create_standing_grant("Operator", "execute_action", 3, "feed")

    payload = _reviews_payload(board_store)

    assert payload["summary"]["total"] == 2
    assert len(payload["notifications"]) == 1
    assert len(payload["pending_approvals"]) == 1
    assert len(payload["standing_grants"]) == 1
    # the notification carries its persisted result data for the UI to act on
    assert "data" in payload["notifications"][0]
