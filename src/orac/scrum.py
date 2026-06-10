from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from orac.agents import RuntimeAgent, build_core_agents
from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.llm import Brain
from orac.models import Board, Task, TaskStatus


@dataclass
class ScrumRunResult:
    cycles: int
    touched_tasks: int
    done_tasks: int


@dataclass
class Scrum:
    brain: Brain
    root: Path | str | None = None
    originate_when_idle: bool = False
    agents: list[RuntimeAgent] = field(init=False)
    store: BrokerStore | None = field(init=False, default=None)
    broker: ToolBroker | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.root is not None:
            self.store = BrokerStore(self.root).init()
            self.broker = ToolBroker.from_store(self.store, repo_root=self.root)
        self.agents = build_core_agents(self.brain, self.broker)

    @property
    def council_agents(self) -> list[RuntimeAgent]:
        """The review-loop agents. Doer subagents (e.g. Builder) are excluded —
        they act when spawned, not in the round-robin council loop."""
        return [agent for agent in self.agents if agent.profile.kind == "council"]

    def plan_sprint(self, board: Board, capacity: int) -> list[Task]:
        planned: list[Task] = []
        used = 0
        for task in board.tasks:
            if task.status != TaskStatus.BACKLOG:
                continue
            if used + task.points > capacity:
                continue
            planned.append(task)
            used += task.points
        for task in planned:
            task.add_log("system", f"Selected for sprint plan within capacity {capacity}.")
        return planned

    SKIP_STATUSES = {
        TaskStatus.CLARIFYING,
        TaskStatus.DONE,
        TaskStatus.BLOCKED,
        TaskStatus.PENDING_APPROVAL,
    }

    def run(self, board: Board, cycles: int = 1) -> ScrumRunResult:
        touched: set[str] = set()
        for _ in range(cycles):
            for task in list(board.tasks):
                if task.parent_id is not None and "contract" in task.metadata:
                    # Doer subtasks (Builder children) are driven by their
                    # runner, not the council round-robin.
                    continue
                if task.status == TaskStatus.PENDING_APPROVAL:
                    if self._resume_if_resolved(task):
                        touched.add(task.id)
                if task.status in self.SKIP_STATUSES:
                    continue
                if self._build_if_goal_task(board, task):
                    touched.add(task.id)
                    continue
                before = (task.status, len(task.work_log))
                for agent in self.council_agents:
                    agent.work(task)
                after = (task.status, len(task.work_log))
                if after != before:
                    touched.add(task.id)
        if self.originate_when_idle:
            originated = self._originate_if_idle(board)
            if originated is not None:
                touched.add(originated)
        done = sum(1 for task in board.tasks if task.status == TaskStatus.DONE)
        return ScrumRunResult(cycles=cycles, touched_tasks=len(touched), done_tasks=done)

    def _build_if_goal_task(self, board: Board, task: Task) -> bool:
        """Goal tasks get really built by a Builder session, not theatrically
        advanced by the council state machine."""
        if self.broker is None or self.root is None:
            return False
        if "build_goal" not in task.metadata or task.status != TaskStatus.READY:
            return False
        from orac.subtasks import run_goal_build

        task.transition(TaskStatus.IN_PROGRESS)
        child = run_goal_build(
            board=board,
            parent=task,
            goal=str(task.metadata["build_goal"]),
            acceptance_criteria=tuple(task.acceptance_criteria),
            brain=self.brain,
            broker=self.broker,
            repo_root=str(self.root),
        )
        if child.status == TaskStatus.DONE and task.status != TaskStatus.BLOCKED:
            task.transition(TaskStatus.DONE)
        return True

    def _originate_if_idle(self, board: Board) -> str | None:
        """Initiative: when nothing is active, the Optimise driver forms one
        self-improvement goal from telemetry. A driver fault becomes a visible
        BLOCKED task on the board, never a silent skip."""
        if self.store is None or self.root is None:
            return None
        from orac.driver import originate

        try:
            origination = originate(board, self.store, self.brain, self.root)
        except ValueError as exc:
            fault = Task(
                title="Optimise driver failed to originate",
                description=str(exc),
                status=TaskStatus.BLOCKED,
                metadata={"origin": "optimise-driver-fault"},
            )
            board.add_task(fault)
            return fault.id
        return origination.task.id if origination is not None else None

    def _resume_if_resolved(self, task: Task) -> bool:
        """Unpark a task whose approval has been resolved.

        Approved tasks return to the status they held before parking and retry;
        denied tasks are blocked. A still-pending approval leaves the task parked.
        Returns True if the task changed state.
        """
        if self.store is None:
            return False
        info = task.metadata.get("pending_approval")
        if not info:
            return False
        pending = self.store.get_pending(int(info["id"]))
        if pending.status == "approved":
            task.resume_from_approval()
            task.add_log("system", "Approval granted; resuming task.")
            return True
        if pending.status in {"denied", "expired"}:
            task.metadata.pop("pending_approval", None)
            task.transition(TaskStatus.BLOCKED)
            task.add_log("system", f"Approval {pending.status}; task blocked.")
            return True
        return False
