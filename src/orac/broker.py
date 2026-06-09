from __future__ import annotations

from dataclasses import dataclass

from orac.agent_registry import load_agent_profiles, load_tool_specs
from orac.broker_store import BrokerStore
from orac.models import CapabilityRequest, CapabilityResult, CapabilityStatus, Task
from orac.tooling import RegularToolExecutor


@dataclass
class ToolBroker:
    """The single entry point for every tool call.

    The broker is the new foundation the tool layer sits on: agents emit a
    :class:`CapabilityRequest`, the broker decides allowed / denied / pending /
    error, and only on ``allowed`` does it dispatch to a handler. Today the only
    backend is :class:`RegularToolExecutor` (in-memory journaling).

    Grants come either from the ``agents.json`` manifest (in-memory, used by
    tests and the no-DB path) or from a :class:`BrokerStore`. When a store is
    attached, every decision is also written to the durable audit log — the
    foundation the ``pending`` approval path and real adapters build on.
    """

    executor: RegularToolExecutor
    grants: dict[str, frozenset[str]]
    known_tools: frozenset[str]
    store: BrokerStore | None = None

    @classmethod
    def from_manifests(cls, executor: RegularToolExecutor | None = None) -> "ToolBroker":
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
        )

    @classmethod
    def from_store(
        cls, store: BrokerStore, executor: RegularToolExecutor | None = None
    ) -> "ToolBroker":
        """Build a broker whose grants and audit log live in SQLite.

        The store is the source of truth for grants; every decision is recorded
        to the audit log.
        """
        return cls(
            executor=executor or RegularToolExecutor(),
            grants=store.grants(),
            known_tools=cls._known_tools(),
            store=store,
        )

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

        result = self.executor.run(req.tool, task, req.agent, **req.args)
        return CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool=result.name,
            message=result.message,
            data=result.data,
        )
