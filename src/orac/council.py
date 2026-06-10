from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from orac.broker_store import BrokerStore
from orac.models import (
    CapabilityStatus,
    CouncilVerdict,
    LensDecision,
    LensVerdict,
    ReviewContext,
    TaskStatus,
)
from orac.policy import safety_critical_paths_touched

if TYPE_CHECKING:
    from orac.lenses import LensReviewer

# P2/P3: the edge-check council, deterministic lenses only (design §4.2-§4.3).
#
# Each lens is a cheap SQL/state check, so the council convenes on every
# store-backed call. LLM escalation of individual lenses is P5 and is gated by
# the risk class — the deterministic floor here never goes away.
#
# Aggregation (design §4.3): any BLOCK -> denied; else any ESCALATE -> pending
# (routed into the existing approval/park machinery); else allowed. A veto is a
# stop with a recorded reason, not a vote to be averaged.


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# Generous defaults: brakes against runaway loops, not friction for normal work.
DEFAULT_DAILY_RATE_CAP = 200
DEFAULT_REPEAT_THRESHOLD = 30

# Tools whose exact repetition is meaningless duplicate work. Repetition of
# reads, status checks, or pushes-after-new-commits is normal and not listed.
DUPLICATE_CHECKED_TOOLS = frozenset({"repo.write_file"})


@dataclass
class Council:
    """The four lenses as deterministic checks.

    ``store=None`` (manifest/no-DB brokers) degrades the state-backed lenses to
    PASS; Intent's closed-task guard still applies since it needs only the task.
    """

    store: BrokerStore | None = None
    daily_rate_cap: int = DEFAULT_DAILY_RATE_CAP
    repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD
    # P5 cognition layer. None -> deterministic floor only (tests, no-DB path).
    # When set, the three judgement lenses also reason over consequential edges.
    llm: "LensReviewer | None" = None

    def review(self, ctx: ReviewContext) -> CouncilVerdict:
        verdicts: list[LensVerdict] = [
            self._intent(ctx),
            self._optimise(ctx),
            self._simples(ctx),
            self._efficiency(ctx),
            self._sentinel(ctx),
        ]
        if self.llm is not None and self.llm.applies(ctx):
            verdicts.extend(self.llm.review(ctx))
        lenses = tuple(verdicts)
        blocks = [v for v in verdicts if v.decision is LensDecision.BLOCK]
        escalations = [v for v in verdicts if v.decision is LensDecision.ESCALATE]
        if blocks:
            return CouncilVerdict(
                status=CapabilityStatus.DENIED,
                lenses=lenses,
                reason="; ".join(v.reason for v in blocks),
            )
        if escalations:
            return CouncilVerdict(
                status=CapabilityStatus.PENDING,
                lenses=lenses,
                reason="; ".join(v.reason for v in escalations),
            )
        return CouncilVerdict(
            status=CapabilityStatus.ALLOWED,
            lenses=lenses,
            reason="council: all lenses pass",
        )

    def _intent(self, ctx: ReviewContext) -> LensVerdict:
        """Goal drift: acting on a closed task serves no locked goal."""
        if ctx.task.status in {TaskStatus.DONE, TaskStatus.BLOCKED}:
            return LensVerdict(
                lens="Intent",
                decision=LensDecision.BLOCK,
                reason=f"Intent: task {ctx.task.id} is {ctx.task.status.value}; "
                "further action would drift past its goal.",
            )
        return LensVerdict(lens="Intent", decision=LensDecision.PASS, reason="on goal")

    def _optimise(self, ctx: ReviewContext) -> LensVerdict:
        """Fair share: a runaway tool burning the daily band gets escalated."""
        if self.store is None:
            return LensVerdict(lens="Optimise", decision=LensDecision.PASS, reason="no usage state")
        used = self.store.rate_count(ctx.request.agent, ctx.request.tool, today_utc())
        if used >= self.daily_rate_cap:
            return LensVerdict(
                lens="Optimise",
                decision=LensDecision.ESCALATE,
                reason=f"Optimise: {ctx.request.agent} has used {ctx.request.tool} "
                f"{used}x today (cap {self.daily_rate_cap}); over the fair-share band.",
            )
        return LensVerdict(lens="Optimise", decision=LensDecision.PASS, reason="within band")

    def _simples(self, ctx: ReviewContext) -> LensVerdict:
        """Rebuild-or-keep proxy: hammering one tool on one task suggests
        patch-churn — the shape should be reviewed instead of patched again."""
        if self.store is None:
            return LensVerdict(lens="Simple", decision=LensDecision.PASS, reason="no history")
        repeats = self.store.audit_count(
            ctx.request.agent, ctx.request.tool, ctx.request.task_id
        )
        if repeats >= self.repeat_threshold:
            return LensVerdict(
                lens="Simple",
                decision=LensDecision.ESCALATE,
                reason=f"Simple: {ctx.request.tool} used {repeats}x on task "
                f"{ctx.request.task_id}; patch-churn suspected — rebuild-or-keep "
                "review needed.",
            )
        return LensVerdict(lens="Simple", decision=LensDecision.PASS, reason="shape holds")

    def _efficiency(self, ctx: ReviewContext) -> LensVerdict:
        """Duplicate work: re-doing an identical, already-successful write."""
        if self.store is None or ctx.request.tool not in DUPLICATE_CHECKED_TOOLS:
            return LensVerdict(lens="Efficiency", decision=LensDecision.PASS, reason="no duplicate")
        dupes = self.store.audit_count_exact(
            ctx.request.agent,
            ctx.request.tool,
            ctx.request.task_id,
            json.dumps(ctx.request.args, sort_keys=True),
        )
        if dupes >= 1:
            return LensVerdict(
                lens="Efficiency",
                decision=LensDecision.BLOCK,
                reason=f"Efficiency: identical {ctx.request.tool} already performed "
                f"on task {ctx.request.task_id}; duplicate work.",
            )
        return LensVerdict(lens="Efficiency", decision=LensDecision.PASS, reason="no duplicate")

    def _sentinel(self, ctx: ReviewContext) -> LensVerdict:
        """Self-modification guard (design §8.7): a write or commit touching the
        files that enforce the safety model escalates to a human even for the
        Builder, regardless of reversibility — the system must not weaken its own
        brakes or widen its own privileges under auto+notify.

        Deterministic and store-free, so it holds on the no-DB path too. Once a
        human approves the exact request, the durable approval clears it (the
        broker's ``_check_approval`` short-circuits the re-issued call), so the
        guard escalates without trapping the work forever.
        """
        touched = safety_critical_paths_touched(ctx.request.tool, ctx.request.args)
        if touched:
            return LensVerdict(
                lens="Sentinel",
                decision=LensDecision.ESCALATE,
                reason=f"Sentinel: {ctx.request.tool} would modify safety-critical "
                f"file(s) {touched}; self-modification of the governor or grant "
                "seed needs human approval regardless of reversibility.",
            )
        return LensVerdict(
            lens="Sentinel", decision=LensDecision.PASS, reason="no safety-critical path"
        )
