from __future__ import annotations

from dataclasses import dataclass, field

from orac.agents import RuntimeAgent, build_core_agents
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
    agents: list[RuntimeAgent] = field(init=False)

    def __post_init__(self) -> None:
        self.agents = build_core_agents(self.brain)

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

    def run(self, board: Board, cycles: int = 1) -> ScrumRunResult:
        touched: set[str] = set()
        for _ in range(cycles):
            for task in board.tasks:
                if task.status in {TaskStatus.CLARIFYING, TaskStatus.DONE, TaskStatus.BLOCKED}:
                    continue
                before = (task.status, len(task.work_log))
                for agent in self.agents:
                    agent.work(task)
                after = (task.status, len(task.work_log))
                if after != before:
                    touched.add(task.id)
        done = sum(1 for task in board.tasks if task.status == TaskStatus.DONE)
        return ScrumRunResult(cycles=cycles, touched_tasks=len(touched), done_tasks=done)
