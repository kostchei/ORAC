from __future__ import annotations

from orac.broker import ToolBroker
from orac.broker_store import MAX_SUBAGENTS, BrokerStore
from orac.metrics import compute_metrics, render_metrics
from orac.models import CapabilityRequest, CapabilityStatus, Task
from orac.self_tune import DECOMPOSE_THRESHOLD_KEY


def _store(tmp_path) -> BrokerStore:
    (tmp_path / ".orac").mkdir()
    return BrokerStore(tmp_path).init()


def test_metrics_roll_up_brokered_calls(tmp_path) -> None:
    store = _store(tmp_path)
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(title="t")
    # an allowed read (Builder may list skills) and a denied write (Intent may not)
    allowed = broker.request(
        CapabilityRequest(agent="Builder", tool="skill.list", task_id=task.id, args={}),
        task,
    )
    assert allowed.status is CapabilityStatus.ALLOWED
    res = broker.request(
        CapabilityRequest(agent="Intent", tool="skill.create", task_id=task.id, args={"name": "x", "content": "c"}),
        task,
    )
    assert res.status is CapabilityStatus.DENIED

    m = compute_metrics(store)
    assert m["audit"]["total"] == 2
    assert m["audit"]["by_status"].get("allowed") == 1
    assert m["audit"]["by_status"].get("denied") == 1
    assert m["roster"]["cap"] == MAX_SUBAGENTS
    assert m["roster"]["in_use"] == 0


def test_metrics_reflects_current_tuning(tmp_path) -> None:
    store = _store(tmp_path)
    assert compute_metrics(store)["tuning"]["decompose_points_threshold"] == 1
    store.set_tunable(DECOMPOSE_THRESHOLD_KEY, "3")
    assert compute_metrics(store)["tuning"]["decompose_points_threshold"] == 3


def test_render_is_stable_text(tmp_path) -> None:
    store = _store(tmp_path)
    text = render_metrics(compute_metrics(store))
    assert "ORAC metrics" in text
    assert "roster:" in text
    assert "decompose threshold" in text
