from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.intent_gate import IntentGate
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
    # Route doer sessions per docs/model-selection.md (work-kind model slots,
    # escalate-to-foundation after a local failure). Off by default so explicit
    # brain choices (tests, CLI --brain) stay exact.
    route_models: bool = False
    # Activate the council's P5 cognition layer: the three judgement lenses
    # reason over consequential edges on a small local model. Off by default so
    # tests and explicit-brain runs keep the deterministic floor only.
    llm_lenses: bool = False
    store: BrokerStore | None = field(init=False, default=None)
    broker: ToolBroker | None = field(init=False, default=None)
    gate: IntentGate = field(init=False)

    def __post_init__(self) -> None:
        if self.root is not None:
            self.store = BrokerStore(self.root).init()
            self.broker = ToolBroker.from_store(
                self.store, repo_root=self.root, council_brain=self._lens_brain()
            )
        self.gate = IntentGate()

    def _lens_brain(self) -> Brain | None:
        if not self.llm_lenses:
            return None
        from orac.model_policy import ModelPolicyStore, lens_brain
        from orac.storage import BoardStore

        return lens_brain(ModelPolicyStore(BoardStore(self.root)))

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
        """One tick per task, one path each: resume an approved task, gate an
        unlocked one, or build a locked-and-ready goal task. The fake council
        round-robin is gone — nothing advances task status by theatre."""
        touched: set[str] = set()
        for _ in range(cycles):
            for task in list(board.tasks):
                if task.parent_id is not None and "contract" in task.metadata:
                    # Doer subtasks (Builder children) are driven by their
                    # runner, not by this loop.
                    continue
                if task.status == TaskStatus.PENDING_APPROVAL:
                    if self._resume_if_resolved(task):
                        touched.add(task.id)
                    continue
                if task.status in {TaskStatus.DONE, TaskStatus.BLOCKED}:
                    continue
                if task.status in IntentGate.OWNED_STATUSES:
                    # Pre-work front door: clarify until intent locks, then the
                    # gate releases the task to READY with goal + work_kind.
                    if self.gate.advance(task):
                        touched.add(task.id)
                    continue
                # Locked + READY goal task: the real doer session does the work.
                if self._build_if_goal_task(board, task):
                    touched.add(task.id)
        if self.originate_when_idle:
            originated = self._originate_if_idle(board)
            if originated is not None:
                touched.add(originated)
        done = sum(1 for task in board.tasks if task.status == TaskStatus.DONE)
        return ScrumRunResult(cycles=cycles, touched_tasks=len(touched), done_tasks=done)

    def _should_decompose(self, task: Task) -> bool:
        """Whether a goal earns the decomposition fan-out over a single doer.

        Decomposition is a cost — trivial work stays single-doer (rugged
        decomposition §2 small-goal bypass). The signals are structural, not
        string-sniffing: an explicit ``decompose`` flag overrides either way;
        otherwise a goal earns a fan-out when it is estimated large (points > 1)
        or its description spells out several steps (> 5 lines).
        """
        if self.broker is None or self.broker.store is None:
            return False
        flag = task.metadata.get("decompose")
        if flag is True:
            return True
        if flag is False:
            return False
        if task.points > 1:
            return True
        return len((task.description or "").splitlines()) > 5

    def _build_if_goal_task(self, board: Board, task: Task) -> bool:
        """Goal tasks are really executed by their kind's doer session, not
        theatrically advanced by the council state machine. A goal that earns it
        (see ``_should_decompose``) fans out across subagents; otherwise one doer
        owns it."""
        if self.broker is None or self.root is None:
            return False
        if task.work_kind is None or "goal" not in task.metadata:
            return False
        if task.status != TaskStatus.READY:
            return False

        task.transition(TaskStatus.IN_PROGRESS)
        # A UI goal declares the URL of its running app in metadata; threading it
        # into context is what arms browser.verify_local_app for that goal (absent
        # it, the frontend verifier is a no-op pass — see work.py). cdp_url is
        # optional; the adapter defaults to the documented local endpoint.
        context: dict[str, str] = {"repo_root": str(self.root)}
        for key in ("app_url", "cdp_url"):
            value = task.metadata.get(key)
            if value:
                context[key] = str(value)
        goal = str(task.metadata["goal"])

        if self._should_decompose(task):
            from orac.work import run_orchestrated_goal

            # The orchestrator that PROPOSES the fan-out runs on the rotating
            # foundation brain (the high-leverage planning call); the fan-out
            # doers run on the local workhorse — the agent fans subtasks out to
            # local (docs/model-selection.md ROUTING).
            run_orchestrated_goal(
                board=board,
                parent=task,
                goal=goal,
                intent=task.description or goal,
                work_kind=task.work_kind,
                brain=self._foundation_brain(),
                broker=self.broker,
                context=context,
                child_brain=self._session_brain(task),
            )
            # run_orchestrated_goal settles the parent against the intent ledger
            # (DONE when covered, BLOCKED when a slice is); only escalation is ours.
            if task.status == TaskStatus.BLOCKED:
                self._maybe_escalate(task)
            return True

        from orac.work import run_goal_task

        child = run_goal_task(
            board=board,
            parent=task,
            goal=goal,
            acceptance_criteria=tuple(task.acceptance_criteria),
            work_kind=task.work_kind,
            brain=self._session_brain(task),
            broker=self.broker,
            context=context,
        )
        if child.status == TaskStatus.DONE and task.status != TaskStatus.BLOCKED:
            task.transition(TaskStatus.DONE)
        elif child.status == TaskStatus.BLOCKED:
            self._maybe_escalate(task)
        return True

    def _session_brain(self, task: Task) -> Brain:
        if not self.route_models:
            return self.brain
        from orac.model_policy import ModelPolicyStore, session_brain_for
        from orac.storage import BoardStore

        return session_brain_for(ModelPolicyStore(BoardStore(self.root)), task)

    def _foundation_brain(self) -> Brain:
        """The brain for high-leverage 'foundation'-routed calls (origination and
        decomposition planning): a rotating frontier model. Falls back to the
        Scrum's own brain when model routing is off (tests / explicit --brain)."""
        if not self.route_models:
            return self.brain
        from orac.model_policy import ModelPolicyStore, foundation_brain_for
        from orac.storage import BoardStore

        return foundation_brain_for(ModelPolicyStore(BoardStore(self.root)))

    def _maybe_escalate(self, task: Task) -> None:
        """Two local failures trigger escalation to a browser foundation provider.

        Failure 1 → retry locally (different random seed, same model).
        Failure 2 → assign the next round-robin browser provider and requeue.
        After the browser also fails → stays BLOCKED for the human.
        """
        if not self.route_models:
            return
        # Browser already tried and failed — leave it BLOCKED for the human.
        if task.metadata.get("escalated"):
            return

        failures = int(task.metadata.get("local_failures", 0)) + 1
        task.metadata["local_failures"] = failures

        if failures < 2:
            task.transition(TaskStatus.READY)
            task.add_log(
                "system",
                f"Local session failed (attempt {failures}/2); retrying locally.",
            )
            return

        # Two local failures: escalate to the next browser provider if available.
        from orac.model_policy import ModelPolicyStore, can_escalate, next_browser_provider  # noqa: PLC0415
        from orac.storage import BoardStore  # noqa: PLC0415

        policy_store = ModelPolicyStore(BoardStore(self.root))
        if not can_escalate(policy_store):
            return  # no escalation path; task stays BLOCKED

        provider = next_browser_provider(policy_store)
        task.metadata["escalated"] = True
        task.metadata["browser_provider"] = provider
        task.transition(TaskStatus.READY)
        task.add_log(
            "system",
            f"Local failed twice; escalating to browser (provider={provider}).",
        )

    def _originate_if_idle(self, board: Board) -> str | None:
        """Initiative: when nothing is active, the Optimise driver forms one
        self-improvement goal from telemetry. A driver fault becomes a visible
        BLOCKED task on the board, never a silent skip."""
        if self.store is None or self.root is None:
            return None
        from orac.driver import originate

        try:
            # Origination is a high-leverage 'foundation'-routed call: form the
            # one self-improvement goal on the rotating frontier brain, not the
            # local workhorse (docs/model-selection.md ROUTING['origination']).
            origination = originate(board, self.store, self._foundation_brain(), self.root)
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
