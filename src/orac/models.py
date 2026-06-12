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
    PENDING_APPROVAL = "pending_approval"


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
    parent_id: str | None = None
    # Which capability category does this work belong to (code / comms / media /
    # physical / event)? None = not a goal-driven task. See orac.work.WORK_KINDS.
    work_kind: str | None = None
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

    def park_for_approval(self, pending_id: int, resume_status: TaskStatus) -> None:
        """Park the task waiting on a human approval.

        The pending approval id and the status to return to are stored in
        metadata so the loop can durably resume the task once the approval is
        resolved — it survives a save/load round-trip of the board.
        """
        self.metadata["pending_approval"] = {
            "id": pending_id,
            "resume_status": resume_status.value,
        }
        self.transition(TaskStatus.PENDING_APPROVAL)

    def resume_from_approval(self) -> TaskStatus:
        """Restore the status the task held before it parked."""
        info = self.metadata.pop("pending_approval", None)
        if info is None:
            raise ValueError("Task is not parked for approval.")
        resume_status = TaskStatus(info["resume_status"])
        self.transition(resume_status)
        return resume_status

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
            parent_id=data.get("parent_id"),
            work_kind=data.get("work_kind"),
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
    # Monotonic save counter. BoardStore.save refuses to overwrite a revision
    # it did not load (StaleBoardError), so concurrent writers cannot silently
    # destroy each other's updates.
    revision: int = 0

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
            "revision": self.revision,
            "tasks": [task.to_dict() for task in self.tasks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Board":
        return cls(
            tasks=[Task.from_dict(item) for item in data.get("tasks", [])],
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            revision=int(data.get("revision", 0)),
        )


# --- Edge-check council contracts (P0) --------------------------------------
#
# Pure data contracts for the edge-check council (see
# docs/edge-check-council-design.md). P0 defines the vocabulary only — no
# behaviour. The classification logic that *produces* a RiskClass and the
# council that *produces* a CouncilVerdict arrive in P1/P2.


class EdgeKind(StrEnum):
    """A boundary crossing the broker mediates (design §4.1)."""

    DISPATCH = "dispatch"        # orchestrator -> subagent
    TOOL_CALL = "tool_call"      # subagent -> tool/adapter
    TOOL_CHAIN = "tool_chain"    # tool -> tool (output feeds the next)
    RETURN = "return"            # subagent -> parent (result roll-up)


class LensDecision(StrEnum):
    """One reviewer's call on an edge (design §4.3)."""

    PASS = "pass"            # no objection
    BLOCK = "block"          # hard veto -> denied
    ESCALATE = "escalate"    # needs human / higher council -> pending


class Reversibility(StrEnum):
    """First risk axis (design §4.4)."""

    REVERSIBLE = "reversible"
    HARD = "hard"                # hard-to-reverse
    IRREVERSIBLE = "irreversible"


class Externality(StrEnum):
    """Second risk axis (design §4.4)."""

    LOCAL = "local"
    EXTERNAL_PRIVATE = "external_private"
    EXTERNAL_PUBLIC = "external_public"
    FINANCIAL = "financial"
    PHYSICAL = "physical"


@dataclass(frozen=True)
class RiskClass:
    """The (reversibility x externality) pair an edge carries.

    P0 defines the type; ``policy.py::risk_class`` (P1) classifies a request into
    one and the throttle table derives the approval requirement from it.
    """

    reversibility: Reversibility
    externality: Externality


@dataclass(frozen=True)
class ReviewContext:
    """Everything a lens needs to judge one edge (design §5)."""

    edge: EdgeKind
    request: CapabilityRequest
    task: Task
    risk: RiskClass


@dataclass(frozen=True)
class LensVerdict:
    """One lens's verdict on a ReviewContext."""

    lens: str                # "Intent" | "Optimise" | "Simple" | "Efficiency"
    decision: LensDecision
    reason: str


@dataclass(frozen=True)
class CouncilVerdict:
    """The aggregated council decision the broker turns into a CapabilityResult.

    Aggregation rule (design §4.3): any BLOCK -> denied; else any ESCALATE ->
    pending; else allowed. The per-lens verdicts are retained so every block is
    explainable in the audit log.
    """

    status: CapabilityStatus
    lenses: tuple[LensVerdict, ...]
    reason: str
