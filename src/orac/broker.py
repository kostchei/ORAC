from __future__ import annotations

from dataclasses import dataclass, field

from orac.agent_registry import load_agent_profiles, load_tool_specs
from orac.models import CapabilityRequest, CapabilityResult, CapabilityStatus, Task
from orac.tooling import RegularToolExecutor


@dataclass
class ToolBroker:
    """The single entry point for every tool call.

    The broker is the new foundation the tool layer sits on: agents emit a
    :class:`CapabilityRequest`, the broker decides allowed / denied / pending /
    error, and only on ``allowed`` does it dispatch to a handler. Today the only
    backend is :class:`RegularToolExecutor` (in-memory journaling), and the only
    gate is a static per-agent allow-list held in memory. SQLite-backed grants,
    real adapters, and an approval queue (the ``pending`` path) come later and
    plug in behind this same contract.
    """

    executor: RegularToolExecutor
    grants: dict[str, frozenset[str]]
    known_tools: frozenset[str]

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
        known_tools = frozenset(spec.name for spec in load_tool_specs())
        return cls(
            executor=executor or RegularToolExecutor(),
            grants=grants,
            known_tools=known_tools,
        )

    def request(self, req: CapabilityRequest, task: Task) -> CapabilityResult:
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
