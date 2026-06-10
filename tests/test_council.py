from __future__ import annotations

import subprocess

from orac.agents import build_core_agents
from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.council import Council, today_utc
from orac.llm import RulesBrain
from orac.models import CapabilityRequest, CapabilityStatus, Task, TaskStatus


def _setup(tmp_path, **council_kwargs):
    (tmp_path / ".orac").mkdir()
    (tmp_path / ".gitignore").write_text(".orac/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    store = BrokerStore(tmp_path).init()
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    if council_kwargs:
        broker.council = Council(store=store, **council_kwargs)
    return broker, store


def _req(tool: str, task: Task, agent: str = "Builder", **args) -> CapabilityRequest:
    return CapabilityRequest(agent=agent, tool=tool, task_id=task.id, args=args)


def test_clean_review_allows_and_records_nothing(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    task = Task(title="normal work", status=TaskStatus.IN_PROGRESS)

    result = broker.request(_req("git.status", task, root=str(tmp_path)), task)

    assert result.status is CapabilityStatus.ALLOWED
    assert store.list_reviews() == []  # all-pass reviews are not persisted


def test_intent_lens_blocks_action_on_closed_task(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    task = Task(title="already finished", status=TaskStatus.DONE)

    result = broker.request(_req("git.status", task, root=str(tmp_path)), task)

    assert result.status is CapabilityStatus.DENIED
    assert "drift" in result.message
    reviews = store.list_reviews(task.id)
    assert any(r["lens"] == "Intent" and r["decision"] == "block" for r in reviews)


def test_efficiency_lens_blocks_duplicate_identical_write(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    task = Task(title="write once", status=TaskStatus.IN_PROGRESS)
    args = {"path": str(tmp_path / "x.py"), "content": "X = 1\n"}

    first = broker.request(_req("repo.write_file", task, **args), task)
    assert first.status is CapabilityStatus.ALLOWED

    second = broker.request(_req("repo.write_file", task, **args), task)
    assert second.status is CapabilityStatus.DENIED
    assert "duplicate" in second.message
    reviews = store.list_reviews(task.id)
    assert any(r["lens"] == "Efficiency" and r["decision"] == "block" for r in reviews)

    # a different change to the same file is NOT a duplicate
    third = broker.request(
        _req("repo.write_file", task, path=args["path"], content="X = 2\n"), task
    )
    assert third.status is CapabilityStatus.ALLOWED


def test_optimise_lens_escalates_over_the_daily_band(tmp_path) -> None:
    broker, store = _setup(tmp_path, daily_rate_cap=2)
    task = Task(title="busy", status=TaskStatus.IN_PROGRESS)

    for _ in range(2):
        assert (
            broker.request(_req("git.status", task, root=str(tmp_path)), task).status
            is CapabilityStatus.ALLOWED
        )

    third = broker.request(_req("git.status", task, root=str(tmp_path)), task)
    assert third.status is CapabilityStatus.PENDING
    assert "fair-share" in third.message or "pending" in third.message.lower()
    reviews = store.list_reviews(task.id)
    assert any(r["lens"] == "Optimise" and r["decision"] == "escalate" for r in reviews)


def test_simples_lens_escalates_patch_churn(tmp_path) -> None:
    broker, store = _setup(tmp_path, repeat_threshold=3)
    task = Task(title="churning", status=TaskStatus.IN_PROGRESS)

    for i in range(3):
        assert (
            broker.request(
                _req("repo.write_file", task, path=str(tmp_path / "x.py"), content=f"X = {i}\n"),
                task,
            ).status
            is CapabilityStatus.ALLOWED
        )

    fourth = broker.request(
        _req("repo.write_file", task, path=str(tmp_path / "x.py"), content="X = 99\n"), task
    )
    assert fourth.status is CapabilityStatus.PENDING
    reviews = store.list_reviews(task.id)
    assert any(r["lens"] == "Simple" and r["decision"] == "escalate" for r in reviews)


def test_escalation_clears_after_human_approval(tmp_path) -> None:
    broker, store = _setup(tmp_path, daily_rate_cap=1)
    task = Task(title="capped", status=TaskStatus.IN_PROGRESS)
    args = {"root": str(tmp_path)}

    assert (
        broker.request(_req("git.status", task, **args), task).status
        is CapabilityStatus.ALLOWED
    )
    parked = broker.request(_req("git.status", task, **args), task)
    assert parked.status is CapabilityStatus.PENDING

    store.resolve_pending(parked.data["pending_id"], "approved")

    cleared = broker.request(_req("git.status", task, **args), task)
    assert cleared.status is CapabilityStatus.ALLOWED


def test_council_escalation_parks_task_end_to_end(tmp_path) -> None:
    # P3 exit criterion: a council ESCALATE flows through the agent path and
    # parks the task via the existing approval machinery.
    broker, store = _setup(tmp_path, daily_rate_cap=1)
    agent = next(a for a in build_core_agents(RulesBrain(), broker) if a.name == "Builder")
    task = Task(title="loop work", status=TaskStatus.IN_PROGRESS)

    agent._act = lambda t: bool(
        agent._use(t, "git.status", root=str(tmp_path))
    )
    agent.work(task)  # first call: within band, allowed
    assert task.status == TaskStatus.IN_PROGRESS

    agent.work(task)  # second call: over band -> escalate -> park
    assert task.status == TaskStatus.PENDING_APPROVAL

    # the verdict trail explains exactly which lens parked it
    reviews = store.list_reviews(task.id)
    assert any(r["lens"] == "Optimise" and r["decision"] == "escalate" for r in reviews)


def test_rate_counter_is_bumped_by_dispatch(tmp_path) -> None:
    broker, store = _setup(tmp_path)
    task = Task(title="counted", status=TaskStatus.IN_PROGRESS)

    broker.request(_req("git.status", task, root=str(tmp_path)), task)
    broker.request(_req("git.status", task, root=str(tmp_path)), task)

    assert store.rate_count("Builder", "git.status", today_utc()) == 2
