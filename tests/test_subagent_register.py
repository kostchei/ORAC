from __future__ import annotations

import pytest

from orac.broker_store import MAX_SUBAGENTS, BrokerStore


def _store(tmp_path) -> BrokerStore:
    (tmp_path / ".orac").mkdir()
    return BrokerStore(tmp_path).init()


def _admit(store: BrokerStore, **kw) -> int:
    return store.admit_subagent(
        parent_task_id=kw.get("parent", "p1"),
        profile_slug=kw.get("slug", "builder"),
        instruction=kw.get("instruction", "do the thing"),
        intent=kw.get("intent", "ship the slice"),
        resource_slice=kw.get("slice", 0.25),
        cap=kw.get("cap", MAX_SUBAGENTS),
    )


def test_admit_creates_an_active_subagent(tmp_path) -> None:
    store = _store(tmp_path)
    sid = _admit(store)

    (sa,) = store.list_subagents()
    assert sa.id == sid
    assert sa.status == "active"
    assert sa.intent == "ship the slice"
    assert sa.resource_slice == 0.25


def test_default_free_slots_is_the_full_cap(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.subagent_free_slots() == MAX_SUBAGENTS
    assert store.subagent_roster_count() == 0


def test_admission_decrements_free_slots(tmp_path) -> None:
    store = _store(tmp_path)
    _admit(store)
    _admit(store)
    assert store.subagent_roster_count() == 2
    assert store.subagent_free_slots() == MAX_SUBAGENTS - 2


def test_retired_and_done_subagents_free_their_slots(tmp_path) -> None:
    store = _store(tmp_path)
    a = _admit(store)
    b = _admit(store)
    assert store.subagent_free_slots() == MAX_SUBAGENTS - 2

    store.set_subagent_status(a, "done")
    store.set_subagent_status(b, "retired")

    # done/retired are no longer live, so the slots return to the roster
    assert store.subagent_roster_count() == 0
    assert store.subagent_free_slots() == MAX_SUBAGENTS


def test_roster_is_capped_and_admission_fails_closed(tmp_path) -> None:
    store = _store(tmp_path)
    _admit(store, cap=2)
    _admit(store, cap=2)

    with pytest.raises(RuntimeError, match="roster is full"):
        _admit(store, cap=2)

    # freeing one slot lets the next in
    live = store.list_subagents(status="active")[0]
    store.set_subagent_status(live.id, "done")
    assert _admit(store, cap=2) > 0


def test_active_slice_total_sums_only_active(tmp_path) -> None:
    store = _store(tmp_path)
    a = _admit(store, slice=0.25)
    _admit(store, slice=0.25)
    assert store.active_slice_total() == 0.5

    store.set_subagent_status(a, "done")  # done no longer counts toward the band
    assert store.active_slice_total() == 0.25


def test_invalid_status_and_unknown_id_raise(tmp_path) -> None:
    store = _store(tmp_path)
    sid = _admit(store)
    with pytest.raises(ValueError):
        store.set_subagent_status(sid, "nonsense")
    with pytest.raises(KeyError):
        store.set_subagent_status(9999, "done")
