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
    agents: list[RuntimeAgent] = field(init=False)
    store: BrokerStore | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        broker = None
        if self.root is not None:
            self.store = BrokerStore(self.root).init()
            broker = ToolBroker.from_store(self.store)
        self.agents = build_core_agents(self.brain, broker)

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
            for task in board.tasks:
                if task.status == TaskStatus.PENDING_APPROVAL:
                    if self._resume_if_resolved(task):
                        touched.add(task.id)
                if task.status in self.SKIP_STATUSES:
                    continue
                before = (task.status, len(task.work_log))
                for agent in self.agents:
                    agent.work(task)
                after = (task.status, len(task.work_log))
                if after != before:
                    touched.add(task.id)
        done = sum(1 for task in board.tasks if task.status == TaskStatus.DONE)
        return ScrumRunResult(cycles=cycles, touched_tasks=len(touched), done_tasks=done)

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
