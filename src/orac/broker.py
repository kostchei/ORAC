from __future__ import annotations

from dataclasses import dataclass, field

from pathlib import Path

from orac.adapters import Adapter, default_adapters
from orac.agent_registry import load_agent_profiles, load_tool_specs
from orac.broker_store import BrokerStore
from orac.code_adapters import code_adapters_for
from orac.council import Council, today_utc
from orac.models import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    EdgeKind,
    LensDecision,
    ReviewContext,
    Task,
)
from orac.policy import ApprovalMode, approval_mode_for, risk_class
from orac.tooling import RegularToolExecutor


@dataclass
class ToolBroker:
    """The single entry point for every tool call.

    The broker is the new foundation the tool layer sits on: agents emit a
    :class:`CapabilityRequest`, the broker decides allowed / denied / pending /
    error, and only on ``allowed`` does it dispatch to a handler. Two backends
    sit behind it: :class:`RegularToolExecutor` (in-memory journaling) and the
    real adapters (e.g. ``fs_read``, the ``repo.*``/``git.*`` code tools).

    Grants come either from the ``agents.json`` manifest (in-memory, used by
    tests and the no-DB path) or from a :class:`BrokerStore`. When a store is
    attached, every decision is written to the durable audit log. Whether a call
    runs, notifies, or parks for approval is decided by the risk model
    (:mod:`orac.policy`), not a hardcoded list.
    """

    executor: RegularToolExecutor
    grants: dict[str, frozenset[str]]
    known_tools: frozenset[str]
    adapters: dict[str, Adapter] = field(default_factory=dict)
    store: BrokerStore | None = None
    council: Council | None = None

    @classmethod
    def from_manifests(
        cls,
        executor: RegularToolExecutor | None = None,
        repo_root: Path | str | None = None,
    ) -> "ToolBroker":
        """Build a broker whose allow-list is derived from ``agents.json``.

        Each agent is granted exactly the tools its profile declares. This makes
        the previously decorative ``tools: [...]`` arrays the enforced source of
        truth.
        """
        grants = {
            profile.name: frozenset(profile.tools) for profile in load_agent_profiles()
        }
        return cls(
            executor=executor or RegularToolExecutor(),
            grants=grants,
            known_tools=cls._known_tools(),
            adapters=cls._adapters(repo_root),
        )

    @classmethod
    def from_store(
        cls,
        store: BrokerStore,
        executor: RegularToolExecutor | None = None,
        repo_root: Path | str | None = None,
    ) -> "ToolBroker":
        """Build a broker whose grants and audit log live in SQLite.

        The store is the source of truth for grants; every decision is recorded
        to the audit log, and approval-gated tools route through the queue. When
        ``repo_root`` is given, the Builder's code adapters are registered,
        confined to that root.
        """
        return cls(
            executor=executor or RegularToolExecutor(),
            grants=store.grants(),
            known_tools=cls._known_tools(),
            adapters=cls._adapters(repo_root),
            store=store,
            council=Council(store=store),
        )

    @staticmethod
    def _adapters(repo_root: Path | str | None) -> dict[str, Adapter]:
        adapters = default_adapters()
        if repo_root is not None:
            adapters.update(code_adapters_for((repo_root,)))
        return adapters

    @staticmethod
    def _known_tools() -> frozenset[str]:
        return frozenset(spec.name for spec in load_tool_specs())

    def request(self, req: CapabilityRequest, task: Task) -> CapabilityResult:
        result = self._decide(req, task)
        if self.store is not None:
            self.store.record_audit(req, result)
        return result

    def _decide(self, req: CapabilityRequest, task: Task) -> CapabilityResult:
        if req.tool not in self.known_tools:
            return CapabilityResult(
                status=CapabilityStatus.ERROR,
                tool=req.tool,
                message=f"Unknown tool {req.tool!r}.",
            )

        granted = self.grants.get(req.agent, frozenset())
        if req.tool not in granted:
            return CapabilityResult(
                status=CapabilityStatus.DENIED,
                tool=req.tool,
                message=f"Agent {req.agent!r} is not granted tool {req.tool!r}.",
            )

        # Edge-check council (design §4.2-§4.3): four deterministic lenses
        # review the edge. Any BLOCK -> denied with the lens's reason; any
        # ESCALATE -> the existing pending/park machinery (a human approval of
        # the exact request clears it). Per-lens verdicts are persisted whenever
        # the review is not clean.
        risk = risk_class(req.tool, req.args)
        if self.council is not None:
            verdict = self.council.review(
                ReviewContext(edge=EdgeKind.TOOL_CALL, request=req, task=task, risk=risk)
            )
            if self.store is not None and any(
                v.decision is not LensDecision.PASS for v in verdict.lenses
            ):
                self.store.record_review(req, verdict)
            if verdict.status is CapabilityStatus.DENIED:
                return CapabilityResult(
                    status=CapabilityStatus.DENIED, tool=req.tool, message=verdict.reason
                )
            if verdict.status is CapabilityStatus.PENDING:
                gate = self._check_approval(req, reason=verdict.reason)
                if gate is not None:
                    return gate

        # The risk model decides what happens next (design §4.4). Review-after,
        # not ask-before: AUTO and NOTIFY both dispatch immediately; NOTIFY also
        # queues the completed action for retrospective review ("I did X — ok?
        # rollback available"). APPROVE parks for a human first and is reserved
        # for the genuinely irreversible (comms / financial / physical).
        mode = approval_mode_for(req.tool, req.args)
        if mode is ApprovalMode.APPROVE:
            gate = self._check_approval(req)
            if gate is not None:
                return gate

        result = self._dispatch(req, task)
        if self.store is not None:
            self.store.bump_rate(req.agent, req.tool, today_utc())
            if mode is ApprovalMode.NOTIFY:
                self.store.record_notification(req, result)
        return result

    def _check_approval(
        self, req: CapabilityRequest, reason: str | None = None
    ) -> CapabilityResult | None:
        """Return a pending result if the request is not yet approved, else None.

        Approval state is durable, so once a human approves the exact request the
        agent can re-issue it and fall through to dispatch. ``reason`` carries
        why the call was parked (e.g. the escalating lens's explanation) so the
        human reviews a cause, not just a tool name.
        """
        if self.store is None:
            return CapabilityResult(
                status=CapabilityStatus.ERROR,
                tool=req.tool,
                message=f"Tool {req.tool!r} is approval-gated and needs a store.",
            )
        status = self.store.approval_status(req)
        if status == "approved":
            return None
        if status is None:
            pending_id = self.store.create_pending(req)
        else:
            pending_id = self.store.get_pending_id(req)
        message = f"Awaiting human approval for {req.tool!r}."
        if reason:
            message = f"{message} Cause: {reason}"
        return CapabilityResult(
            status=CapabilityStatus.PENDING,
            tool=req.tool,
            message=message,
            data={"pending_id": pending_id},
        )

    def _dispatch(self, req: CapabilityRequest, task: Task) -> CapabilityResult:
        adapter = self.adapters.get(req.tool)
        if adapter is not None:
            result = adapter(req)
        else:
            result = self.executor.run(req.tool, task, req.agent, **req.args)
        return CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool=result.name,
            message=result.message,
            data=result.data,
        )
