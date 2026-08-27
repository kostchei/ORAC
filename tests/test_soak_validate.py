import subprocess

from scripts.soak_validate import _active_ids, _git_state, _new_bad_logs, _seed_canary_task

from orac.broker_store import BrokerStore
from orac.models import Task, TaskStatus
from orac.storage import BoardStore


def test_canary_ignores_existing_logs_and_flags_new_budget_exhaustion(tmp_path) -> None:
    store = BoardStore(tmp_path)
    store.init()
    board = store.load()
    task = Task(title="canary")
    task.add_log("Builder", "Step budget exhausted (12) without done/blocked.")
    board.add_task(task)
    store.save(board)
    offsets = {task.id: 1}

    assert _new_bad_logs(store, offsets) == []

    board = store.load()
    board.get_task(task.id).add_log(
        "Optimise", "Could not produce a usable verdict: truncated JSON"
    )
    store.save(board)

    assert _new_bad_logs(store, offsets) == [
        f"{task.id}/Optimise: Could not produce a usable verdict: truncated JSON"
    ]


def test_canary_active_ids_include_only_live_reservations(tmp_path) -> None:
    store = BrokerStore(tmp_path).init()
    active = store.admit_subagent("parent", "builder", "do", "intent")
    done = store.admit_subagent("parent", "builder", "done", "intent")
    store.set_subagent_status(done, "done")

    assert _active_ids(store) == {active}


def test_canary_git_state_exposes_branch_head_and_dirty_paths(tmp_path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit",
         "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    clean = _git_state(str(tmp_path))
    assert clean["branch"] == "main"
    assert len(clean["head"]) == 40
    assert clean["status"] == ""

    (tmp_path / "new.txt").write_text("dirty", encoding="utf-8")
    assert "?? new.txt" in _git_state(str(tmp_path))["status"]


def test_canary_seeds_one_non_mutating_task_only_when_board_is_idle(tmp_path) -> None:
    store = BoardStore(tmp_path)
    store.init()

    task_id = _seed_canary_task(store)

    assert task_id is not None
    task = store.load().get_task(task_id)
    assert task.status is TaskStatus.READY
    assert task.work_kind == "code"
    assert task.metadata["origin"] == "supervised-canary"
    assert "Do not edit" in task.description
    assert _seed_canary_task(store) is None
