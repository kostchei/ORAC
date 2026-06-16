from __future__ import annotations

from dataclasses import dataclass, field

from pathlib import Path

from orac.adapters import Adapter, default_adapters
from orac.browser_adapters import browser_adapters
from orac.agent_registry import load_agent_profiles, load_tool_specs
from orac.broker_store import BrokerStore
from orac.code_adapters import code_adapters_for
from orac.fs_adapters import fs_adapters_for
from orac.council import Council, today_utc
from orac.llm import Brain
from orac.models import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    EdgeKind,
    LensDecision,
    ReviewContext,
    Task,
)
from orac.policy import ApprovalMode, approval_mode_for, contract_denial, risk_class
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
        council_brain: "Brain | None" = None,
    ) -> "ToolBroker":
        """Build a broker whose grants and audit log live in SQLite.

        The store is the source of truth for grants; every decision is recorded
        to the audit log, and approval-gated tools route through the queue. When
        ``repo_root`` is given, the Builder's code adapters are registered,
        confined to that root.

        ``council_brain`` activates the P5 cognition layer: the three judgement
        lenses reason over consequential edges on this (local) model. ``None``
        leaves the council at its deterministic floor.
        """
        llm = None
        if council_brain is not None:
            from orac.lenses import LensReviewer  # noqa: PLC0415 (avoid import cycle)

            llm = LensReviewer(brain=council_brain, store=store)
        return cls(
            executor=executor or RegularToolExecutor(),
            grants=store.grants(),
            known_tools=cls._known_tools(),
            adapters=cls._adapters(repo_root),
            store=store,
            council=Council(store=store, llm=llm),
        )

    @staticmethod
    def _adapters(repo_root: Path | str | None) -> dict[str, Adapter]:
        adapters = default_adapters()
        # Read-only, repo-independent: the frontend verifier needs only a running
        # browser, not an approved repo root, so it is always available.
        adapters.update(browser_adapters())
        if repo_root is not None:
            adapters.update(code_adapters_for((repo_root,)))
            adapters.update(fs_adapters_for(repo_root))
            from orac.comms_adapters import comms_adapters_for
            adapters.update(comms_adapters_for(repo_root))
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

        # Slice-contract scope (rugged decomposition invariant #4): a doer running
        # a decomposed slice may use only the tools and touch only the paths its
        # contract grants. Empty/absent fields impose no restriction, so plain
        # (non-slice) contracts pass through. Checked before the council so an
        # out-of-scope call is refused as an admission error, not reviewed.
        contract = task.metadata.get("contract") if task is not None else None
        denial = contract_denial(
            req.tool, req.args, contract if isinstance(contract, dict) else None
        )
        if denial is not None:
            return CapabilityResult(
                status=CapabilityStatus.DENIED, tool=req.tool, message=denial
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
        #
        # A standing grant (P6) short-circuits the APPROVE park for pre-authorised
        # recurring intent (the fish-feeder case), rate-capped per day. It still
        # dispatches + notifies, so the human reviews after the fact. It does NOT
        # bypass the council ESCALATE above — the safety floor (Sentinel and the
        # fair-share/churn lenses) is never waived by a standing grant.
        mode = approval_mode_for(req.tool, req.args)
        standing_granted = False
        if mode is ApprovalMode.APPROVE:
            standing_granted = self._standing_grant_clears(req)
            if not standing_granted:
                gate = self._check_approval(req)
                if gate is not None:
                    return gate

        result = self._dispatch(req, task)
        if self.store is not None:
            self.store.bump_rate(req.agent, req.tool, today_utc())
            if mode in (ApprovalMode.NOTIFY, ApprovalMode.APPROVE) or standing_granted:
                self.store.record_notification(req, result)
        return result

    def _standing_grant_clears(self, req: CapabilityRequest) -> bool:
        """True if an active, in-cap standing grant pre-authorises this call.

        The grant's daily cap is counted against the same ``rate_counters`` the
        Optimise lens reads, so a pre-authorised action that has already run its
        cap for the day falls back to the human-approval park rather than running
        unbounded.
        """
        if self.store is None:
            return False
        grant = self.store.standing_grant_for(req.agent, req.tool, req.args)
        if grant is None:
            return False
        used_today = self.store.rate_count(req.agent, req.tool, today_utc())
        return used_today < grant.daily_cap

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
