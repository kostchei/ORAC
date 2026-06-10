from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orac.broker import ToolBroker
from orac.models import Board, CapabilityRequest, CapabilityStatus, Task, TaskStatus

# P4: orchestrator -> Builder spawn (design §4.5/§4.6).
#
# The Roo/OpenClaw lesson, enforced rather than trusted: the child receives an
# explicit contract (instruction-down), works in isolation through the broker,
# and only a summary returns to the parent (summary-up). The return-edge check
# is deterministic for now — the contract's tests must pass — and becomes the
# council review when P2/P3 land.


@dataclass(frozen=True)
class FileWrite:
    """One file the Builder must write, inside the approved repo root."""

    path: str
    content: str


@dataclass(frozen=True)
class SubtaskContract:
    """The instruction handed down to a spawned Builder subtask.

    Self-contained on purpose: the child does not inherit the parent's history.
    Everything the Builder may do is named here; the broker enforces the rest.
    """

    goal: str
    file_writes: tuple[FileWrite, ...]
    acceptance_criteria: tuple[str, ...] = ()
    test_target: str | None = None  # pytest target; None runs the root's suite
    branch: str | None = None       # None derives build/<child-id>

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "file_writes": [{"path": w.path, "content": w.content} for w in self.file_writes],
            "acceptance_criteria": list(self.acceptance_criteria),
            "test_target": self.test_target,
            "branch": self.branch,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubtaskContract":
        return cls(
            goal=str(data["goal"]),
            file_writes=tuple(
                FileWrite(path=str(w["path"]), content=str(w["content"]))
                for w in data["file_writes"]
            ),
            acceptance_criteria=tuple(data.get("acceptance_criteria", ())),
            test_target=data.get("test_target"),
            branch=data.get("branch"),
        )


def spawn_build_subtask(board: Board, parent: Task, contract: SubtaskContract) -> Task:
    """Create the Builder's child task on the board.

    The child carries the contract in metadata, points back at its parent, and
    is assigned to the Builder. Council agents do not round-robin it — it is
    driven by ``execute_build_subtask``.
    """
    child = Task(
        title=f"[build] {contract.goal}",
        description=contract.goal,
        parent_id=parent.id,
        assignee="Builder",
        status=TaskStatus.READY,
        acceptance_criteria=list(contract.acceptance_criteria),
        metadata={"contract": contract.to_dict()},
    )
    board.add_task(child)
    parent.add_log("Orchestrator", f"Spawned build subtask {child.id}: {contract.goal}")
    return child


def execute_build_subtask(
    child: Task, broker: ToolBroker, repo_root: str
) -> dict[str, Any]:
    """Drive the Builder through its contract via the broker, and return the
    summary that rolls up to the parent.

    Checkpoint-first: branch, then path-scoped writes and a single focused
    commit, then the contract's tests. Tests pass -> child DONE; tests fail ->
    child BLOCKED. Every call is an ordinary brokered capability request — the
    Builder gets no privileged path here.
    """
    contract = SubtaskContract.from_dict(child.metadata["contract"])
    branch = contract.branch or f"build/{child.id}"

    def use(tool: str, **args: Any) -> dict[str, Any]:
        result = broker.request(
            CapabilityRequest(agent="Builder", tool=tool, task_id=child.id, args=args),
            child,
        )
        if result.status is not CapabilityStatus.ALLOWED:
            raise RuntimeError(
                f"Builder could not use {tool!r}: {result.status.value} - {result.message}"
            )
        return result.data

    child.transition(TaskStatus.IN_PROGRESS)
    use("git.create_branch", root=repo_root, name=branch)
    for write in contract.file_writes:
        use("repo.write_file", path=write.path, content=write.content)
    commit = use(
        "git.commit",
        root=repo_root,
        message=f"[orac-build {child.id}] {contract.goal}",
        paths=[write.path for write in contract.file_writes],
    )

    test_args: dict[str, Any] = {"root": repo_root}
    if contract.test_target is not None:
        test_args["target"] = contract.test_target
    tests = use("repo.run_tests", **test_args)

    summary: dict[str, Any] = {
        "goal": contract.goal,
        "branch": branch,
        "commit": commit["sha"],
        "files": [write.path for write in contract.file_writes],
        "tests_passed": tests["passed"],
        "test_summary": tests["summary"],
    }
    # Return edge: deterministic acceptance check (council review lands in P2/P3).
    if tests["passed"]:
        child.add_log("Builder", f"Built {contract.goal!r} on {branch}: tests passed.")
        child.transition(TaskStatus.DONE)
    else:
        child.add_log("Builder", f"Build of {contract.goal!r} failed its tests.")
        child.transition(TaskStatus.BLOCKED)
    return summary


def run_build(
    board: Board,
    parent: Task,
    contract: SubtaskContract,
    broker: ToolBroker,
    repo_root: str,
) -> Task:
    """Spawn, execute, and roll the summary up to the parent (summary-up).

    The parent never sees the child's working detail — only the summary, which
    becomes the parent's record of what was built.
    """
    child = spawn_build_subtask(board, parent, contract)
    summary = execute_build_subtask(child, broker, repo_root)
    if summary["tests_passed"]:
        parent.add_log(
            "Orchestrator",
            f"Build subtask {child.id} done: {summary['goal']} "
            f"(commit {summary['commit'][:8]} on {summary['branch']}, tests passed).",
        )
    else:
        parent.add_log(
            "Orchestrator",
            f"Build subtask {child.id} FAILED its tests; parent blocked pending review.",
        )
        parent.transition(TaskStatus.BLOCKED)
    return child
