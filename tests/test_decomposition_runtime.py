from __future__ import annotations

import subprocess
from dataclasses import dataclass

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.models import Board, CapabilityRequest, CapabilityStatus, Task, TaskStatus
from orac.policy import contract_denial
from orac.scrum import Scrum
from orac.work import run_goal_task


# === 1. contract_denial — the pure scope check (broker enforcement core) ======


def test_no_contract_imposes_no_restriction() -> None:
    assert contract_denial("repo.write_file", {"path": "x.py"}, None) is None
    assert contract_denial("repo.write_file", {"path": "x.py"}, {}) is None


def test_forbidden_tool_is_denied() -> None:
    contract = {"forbidden_tools": ["git.create_branch"]}
    assert "forbidden" in contract_denial("git.create_branch", {}, contract)
    assert contract_denial("repo.write_file", {"path": "x"}, contract) is None


def test_allowed_tools_is_an_allowlist_when_non_empty() -> None:
    contract = {"allowed_tools": ["repo.write_file"]}
    assert contract_denial("repo.write_file", {"path": "x"}, contract) is None
    assert "not in the slice contract's allowed_tools" in contract_denial(
        "repo.search", {"query": "x"}, contract
    )


def test_empty_allowed_tools_imposes_no_restriction() -> None:
    assert contract_denial("repo.search", {"query": "x"}, {"allowed_tools": []}) is None


def test_owned_path_exact_and_nested_are_allowed() -> None:
    contract = {"owned_paths_or_resources": ["src/orac/other.py", "tests"]}
    assert contract_denial("repo.write_file", {"path": "src/orac/other.py"}, contract) is None
    # a file nested under an owned directory
    assert contract_denial("repo.write_file", {"path": "tests/test_x.py"}, contract) is None


def test_owned_path_matches_absolute_target_by_suffix() -> None:
    # the builder writes absolute paths; a relative owned entry must still match
    contract = {"owned_paths_or_resources": ["src/orac/other.py"]}
    abs_target = "D:/repo/src/orac/other.py"
    assert contract_denial("repo.write_file", {"path": abs_target}, contract) is None


def test_unowned_path_is_denied() -> None:
    contract = {"owned_paths_or_resources": ["src/orac/other.py", "tests"]}
    reason = contract_denial("repo.write_file", {"path": "src/orac/scrum.py"}, contract)
    assert reason is not None and "not owned" in reason


def test_owned_path_no_partial_segment_match() -> None:
    # owned 'test' must NOT match 'tests/x' (segment-boundary matching)
    contract = {"owned_paths_or_resources": ["test"]}
    assert contract_denial("repo.write_file", {"path": "tests/x.py"}, contract) is not None


def test_git_commit_checks_every_path_in_the_list() -> None:
    contract = {"owned_paths_or_resources": ["tests"]}
    assert contract_denial("git.commit", {"paths": ["tests/a.py"]}, contract) is None
    reason = contract_denial("git.commit", {"paths": ["tests/a.py", "src/x.py"]}, contract)
    assert reason is not None and "src/x.py" in reason


# === 2. the broker enforces the contract at the edge (deny path, no execute) ==


def _repo(tmp_path):
    (tmp_path / ".orac").mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    store = BrokerStore(tmp_path).init()
    store.grant("Builder", "repo.write_file")
    store.grant("Builder", "git.create_branch")
    return ToolBroker.from_store(store, repo_root=tmp_path), store


def _child_with_contract(contract: dict) -> Task:
    return Task(
        title="[code] slice",
        status=TaskStatus.IN_PROGRESS,
        metadata={"contract": {"goal": "g", "kind": "code", **contract}},
    )


def test_broker_denies_write_outside_owned_paths(tmp_path) -> None:
    broker, _ = _repo(tmp_path)
    child = _child_with_contract({"owned_paths_or_resources": ["src/orac/other.py"]})
    req = CapabilityRequest(
        agent="Builder", tool="repo.write_file", task_id=child.id,
        args={"path": "src/orac/scrum.py", "content": "# x"},
    )
    result = broker.request(req, child)
    assert result.status is CapabilityStatus.DENIED
    assert "not owned by the slice contract" in result.message


def test_broker_denies_forbidden_tool(tmp_path) -> None:
    broker, _ = _repo(tmp_path)
    child = _child_with_contract({"forbidden_tools": ["git.create_branch"]})
    req = CapabilityRequest(
        agent="Builder", tool="git.create_branch", task_id=child.id,
        args={"root": str(tmp_path), "name": "b"},
    )
    result = broker.request(req, child)
    assert result.status is CapabilityStatus.DENIED
    assert "forbidden by the slice contract" in result.message


# === 3. the verifier repair loop (opt-in, bounded) ============================


@dataclass
class _MockResult:
    status: str
    summary: str


class _MockSession:
    """Stands in for AgentSession: records contracts seen, always claims done."""

    def __init__(self, *a, **k) -> None:
        self.contracts: list[str] = []

    def run(self, task, contract):
        self.contracts.append(contract)
        return _MockResult(status="done", summary="claimed done")


def test_repair_is_a_new_verified_slice(tmp_path, monkeypatch) -> None:
    (tmp_path / ".orac").mkdir()
    broker = ToolBroker.from_store(BrokerStore(tmp_path).init())
    board = Board()
    parent = Task(title="parent", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    verify_calls = {"n": 0}

    def fake_verify(*a, **k):
        verify_calls["n"] += 1
        if verify_calls["n"] == 1:
            return False, "pytest: 1 failed at line 5"
        return True, "tests passed"

    session = _MockSession()
    import orac.work as work_mod

    monkeypatch.setattr(work_mod, "verify_goal_done", fake_verify)
    monkeypatch.setattr(work_mod, "AgentSession", lambda *a, **k: session)

    child = run_goal_task(
        board, parent, goal="fix the bug", acceptance_criteria=("works",),
        work_kind="code", brain=None, broker=broker,
        context={"repo_root": str(tmp_path)}, max_repairs=2,
    )

    # The slice is satisfied — via a repair, not an in-place re-run.
    assert child.status is TaskStatus.DONE
    assert len(session.contracts) == 2                       # original + repair
    assert "verification_failure" not in session.contracts[0].lower()
    assert "pytest: 1 failed at line 5" in session.contracts[1]  # failure carried in

    # The repair is a NEW slice on the board: a child of the failed slice, itself
    # independently verified (not an invisible loop iteration).
    repair_children = [t for t in board.tasks if t.parent_id == child.id]
    assert len(repair_children) == 1
    assert repair_children[0].status is TaskStatus.DONE
    assert any("repair slice" in log.message.lower() for log in child.work_log)


def test_repair_disabled_by_default_blocks_on_first_failure(tmp_path, monkeypatch) -> None:
    (tmp_path / ".orac").mkdir()
    broker = ToolBroker.from_store(BrokerStore(tmp_path).init())
    board = Board()
    parent = Task(title="parent", status=TaskStatus.IN_PROGRESS)
    board.add_task(parent)

    session = _MockSession()
    import orac.work as work_mod

    monkeypatch.setattr(work_mod, "verify_goal_done", lambda *a, **k: (False, "red"))
    monkeypatch.setattr(work_mod, "AgentSession", lambda *a, **k: session)

    child = run_goal_task(
        board, parent, goal="x", acceptance_criteria=(),
        work_kind="code", brain=None, broker=broker,
        context={"repo_root": str(tmp_path)},  # max_repairs defaults to 0
    )

    assert child.status is TaskStatus.BLOCKED
    assert len(session.contracts) == 1              # no retry


# === 4. scrum routes large goals to the fan-out, small ones to one doer =======


def _scrum(tmp_path) -> Scrum:
    (tmp_path / ".orac").mkdir()
    return Scrum(brain=None, root=tmp_path)


def _goal_task(**over) -> Task:
    meta = {"goal": over.pop("goal", "do the thing")}
    if "decompose" in over:
        meta["decompose"] = over.pop("decompose")
    task = Task(title="goal task", status=TaskStatus.READY, metadata=meta, **over)
    task.work_kind = "code"
    return task


def test_should_decompose_signals(tmp_path) -> None:
    scrum = _scrum(tmp_path)
    assert scrum._should_decompose(_goal_task(points=1)) is False
    assert scrum._should_decompose(_goal_task(points=2)) is True
    assert scrum._should_decompose(_goal_task(description="a\nb\nc\nd\ne\nf")) is True
    # explicit flag overrides either way
    assert scrum._should_decompose(_goal_task(points=8, decompose=False)) is False
    assert scrum._should_decompose(_goal_task(points=1, decompose=True)) is True


def test_build_routes_simple_to_doer_and_complex_to_fanout(tmp_path, monkeypatch) -> None:
    scrum = _scrum(tmp_path)
    monkeypatch.setattr(Scrum, "_session_brain", lambda *a, **k: None)

    calls = {"goal": 0, "orchestrated": 0}
    import orac.work as work_mod

    def fake_goal(*a, **k):
        calls["goal"] += 1
        child = Task(title="child")
        child.transition(TaskStatus.DONE)
        return child

    def fake_orchestrated(*a, **k):
        calls["orchestrated"] += 1
        return []

    monkeypatch.setattr(work_mod, "run_goal_task", fake_goal)
    monkeypatch.setattr(work_mod, "run_orchestrated_goal", fake_orchestrated)

    board = Board()
    simple = _goal_task(points=1)
    board.add_task(simple)
    scrum._build_if_goal_task(board, simple)
    assert calls == {"goal": 1, "orchestrated": 0}

    complex_task = _goal_task(points=3)
    board.add_task(complex_task)
    scrum._build_if_goal_task(board, complex_task)
    assert calls == {"goal": 1, "orchestrated": 1}
