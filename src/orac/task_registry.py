from __future__ import annotations

from dataclasses import dataclass

from orac.models import Board, Task, TaskStatus


@dataclass(frozen=True)
class RegistryStats:
    total: int
    backlog: int
    clarifying: int
    active: int
    done: int
    blocked: int


class TaskRegistry:
    def __init__(self, board: Board) -> None:
        self.board = board

    def add_base_request(self, title: str, description: str, points: int = 1) -> Task:
        task = Task(
            title=title.strip(),
            description=description.strip(),
            points=points,
            metadata={"request_type": "base_request"},
        )
        task.add_log("User", "Base request added.", kind="user")
        self.board.add_task(task)
        return task

    def stats(self) -> RegistryStats:
        active_statuses = {TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW}
        return RegistryStats(
            total=len(self.board.tasks),
            backlog=sum(1 for task in self.board.tasks if task.status == TaskStatus.BACKLOG),
            clarifying=sum(
                1 for task in self.board.tasks if task.status == TaskStatus.CLARIFYING
            ),
            active=sum(1 for task in self.board.tasks if task.status in active_statuses),
            done=sum(1 for task in self.board.tasks if task.status == TaskStatus.DONE),
            blocked=sum(1 for task in self.board.tasks if task.status == TaskStatus.BLOCKED),
        )

    def interactions(self) -> list[dict[str, str]]:
        events: list[dict[str, str]] = []
        for task in self.board.tasks:
            events.append(
                {
                    "task_id": task.id,
                    "task_title": task.title,
                    "agent": "Registry",
                    "kind": "log",
                    "message": f"Task is {task.status.value}.",
                    "created_at": task.updated_at,
                }
            )
            for entry in task.work_log:
                events.append(
                    {
                        "task_id": task.id,
                        "task_title": task.title,
                        "agent": entry.agent,
                        "kind": entry.kind,
                        "message": entry.message,
                        "created_at": entry.created_at,
                    }
                )
        return sorted(events, key=lambda event: event["created_at"], reverse=True)
