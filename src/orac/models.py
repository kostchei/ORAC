from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class TaskStatus(StrEnum):
    BACKLOG = "backlog"
    CLARIFYING = "clarifying"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"


class CapabilityStatus(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    PENDING = "pending"
    ERROR = "error"


@dataclass(frozen=True)
class CapabilityRequest:
    """A structured request from an agent to use one tool.

    This is the single shape every tool call must take once routed through the
    broker. Agents name the capability and supply arguments; they never reach a
    handler directly.
    """

    agent: str
    tool: str
    task_id: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityResult:
    """The broker's verdict on a CapabilityRequest.

    ``status`` is the stable agent-facing contract: an agent must handle all four
    of allowed / denied / pending / error. ``data`` carries the handler payload on
    success and is empty otherwise.
    """

    status: CapabilityStatus
    tool: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkLog:
    agent: str
    message: str
    kind: str = "agent"
    created_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkLog":
        return cls(
            agent=str(data["agent"]),
            message=str(data["message"]),
            kind=str(data.get("kind", "agent")),
            created_at=str(data.get("created_at") or now_iso()),
        )


@dataclass
class Task:
    title: str
    description: str = ""
    points: int = 1
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    status: TaskStatus = TaskStatus.BACKLOG
    assignee: str | None = None
    acceptance_criteria: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    work_log: list[WorkLog] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def add_log(self, agent: str, message: str, kind: str = "agent") -> None:
        self.work_log.append(WorkLog(agent=agent, message=message, kind=kind))
        self.updated_at = now_iso()

    def transition(self, status: TaskStatus) -> None:
        self.status = status
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            description=str(data.get("description", "")),
            points=int(data.get("points", 1)),
            status=TaskStatus(data.get("status", TaskStatus.BACKLOG)),
            assignee=data.get("assignee"),
            acceptance_criteria=list(data.get("acceptance_criteria", [])),
            metadata=dict(data.get("metadata", {})),
            work_log=[WorkLog.from_dict(item) for item in data.get("work_log", [])],
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
        )


@dataclass
class Board:
    tasks: list[Task] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def add_task(self, task: Task) -> None:
        self.tasks.append(task)
        self.updated_at = now_iso()

    def get_task(self, task_id: str) -> Task:
        matches = [task for task in self.tasks if task.id.startswith(task_id)]
        if not matches:
            raise KeyError(f"No task found for id prefix {task_id!r}.")
        if len(matches) > 1:
            raise KeyError(f"Task id prefix {task_id!r} matches more than one task.")
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tasks": [task.to_dict() for task in self.tasks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Board":
        return cls(
            tasks=[Task.from_dict(item) for item in data.get("tasks", [])],
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
        )
