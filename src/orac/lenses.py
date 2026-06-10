from __future__ import annotations

import json
from dataclasses import dataclass, field

from orac.agent_registry import load_agent_profiles
from orac.broker_store import BrokerStore
from orac.council import today_utc
from orac.llm import Brain
from orac.models import LensDecision, LensVerdict, ReviewContext

# P5: the council's cognition layer.
#
# The deterministic lenses in council.py are the floor — cheap SQL/state checks
# that catch the obvious (closed-task drift, exact duplicate, runaway rate).
# This layer sits beside them: each of the three judgement lenses (Optimise,
# Simple, Efficiency) calls an actual — deliberately small, local — model that
# reads ONE edge through its own purpose and returns pass / escalate / block.
# The broker aggregates LLM and deterministic verdicts together, unchanged.
#
# Cost discipline: the council convenes on every store-backed call, but most are
# reads. The model is consulted only on consequential edges (a write, commit,
# push, or revert) — the places where waste, churn, and scope-creep actually
# live. A handful of small-model calls per build, not three per file read.

# The edges worth a model's attention: state-changing artifacts a lens can judge.
LLM_REVIEWED_TOOLS = frozenset(
    {"repo.write_file", "git.commit", "git.push", "git.revert"}
)

# Lens display name -> agent profile slug (whose prompts/<slug>.md is its skill).
LENS_SLUGS: tuple[tuple[str, str], ...] = (
    ("Optimise", "optimiser"),
    ("Simple", "simples"),
    ("Efficiency", "efficiency"),
)

REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["pass", "block", "escalate"]},
        "reason": {"type": "string"},
    },
    "required": ["decision", "reason"],
    "additionalProperties": False,
}

# The decision contract, shared by every lens. The persona above it (the
# prompts/<slug>.md skill) supplies the purpose; this supplies the verdict shape
# and the bias a brake should have.
REVIEW_PROTOCOL = """\
You are acting as ONE review lens on a single tool call an agent is about to
make. You have limited cognition: judge ONLY through your own purpose stated
above, and only this one call. Do not re-judge what other lenses own.

Choose exactly one decision:
- "pass": your lens has no objection.
- "escalate": your lens sees a real concern a human should weigh before this runs.
- "block": your lens is certain, within its domain, that this call is wrong and must not run.

Bias toward "pass" for ordinary work — you are a brake against clear problems in
your domain, not friction. Reserve "block" for certain harm; use "escalate" when
you are unsure but uneasy.

Reply with ONE JSON object and nothing else:
{"decision": "pass|escalate|block", "reason": "<one short sentence>"}"""

_ARG_LIMIT = 1500


@dataclass
class LensReviewer:
    """The LLM cognition layer for the council's three judgement lenses."""

    brain: Brain
    store: BrokerStore | None = None
    _personas: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self._personas:
            self._personas = {p.slug: p.system_prompt for p in load_agent_profiles()}

    def applies(self, ctx: ReviewContext) -> bool:
        """The model is consulted only on consequential, judgeable edges."""
        return ctx.request.tool in LLM_REVIEWED_TOOLS

    def review(self, ctx: ReviewContext) -> list[LensVerdict]:
        return [self._one(name, slug, ctx) for name, slug in LENS_SLUGS]

    def _one(self, name: str, slug: str, ctx: ReviewContext) -> LensVerdict:
        think_json = getattr(self.brain, "think_json", None)
        if not callable(think_json):
            # No fallback to deterministic-only or to PASS: a lens configured to
            # think must have a structured-output brain. A misconfiguration is a
            # loud failure, not a silent wave-through.
            raise RuntimeError(
                "LLM lenses require a brain with structured output (think_json)."
            )
        reply = think_json(name, slug, ctx.task, self._prompt(name, slug, ctx), REVIEW_SCHEMA)
        decision, reason = self._parse(reply)
        return LensVerdict(lens=name, decision=decision, reason=f"{name}: {reason}")

    def _prompt(self, name: str, slug: str, ctx: ReviewContext) -> str:
        persona = self._personas[slug]
        goal = str(ctx.task.metadata.get("goal", ctx.task.title))
        criteria = "\n".join(f"  - {c}" for c in ctx.task.acceptance_criteria) or "  - (none)"
        return (
            f"{persona}\n\n"
            f"{REVIEW_PROTOCOL}\n\n"
            "EDGE UNDER REVIEW:\n"
            f"- tool: {ctx.request.tool}\n"
            f"- agent: {ctx.request.agent}\n"
            f"- task goal: {goal}\n"
            f"- acceptance criteria:\n{criteria}\n"
            f"- arguments:\n{self._args_block(ctx)}\n"
            f"{self._telemetry_block(ctx)}"
            f"\nYour verdict as the {name} lens:"
        )

    def _args_block(self, ctx: ReviewContext) -> str:
        args = dict(ctx.request.args)
        content = args.pop("content", None)
        lines = [f"    {k}: {json.dumps(v, default=str)[:300]}" for k, v in args.items()]
        if content is not None:
            text = str(content)
            shown = text[:_ARG_LIMIT]
            suffix = "" if len(text) <= _ARG_LIMIT else f" …(+{len(text) - _ARG_LIMIT} chars)"
            lines.append(f"    content:\n{shown}{suffix}")
        return "\n".join(lines) or "    (none)"

    def _telemetry_block(self, ctx: ReviewContext) -> str:
        if self.store is None:
            return ""
        on_task = self.store.audit_count(ctx.request.agent, ctx.request.tool, ctx.request.task_id)
        today = self.store.rate_count(ctx.request.agent, ctx.request.tool, today_utc())
        return (
            "- work so far:\n"
            f"    {ctx.request.tool} used {on_task}x on this task; {today}x today across tasks\n"
        )

    def _parse(self, reply: str) -> tuple[LensDecision, str]:
        decision = _parse_json_object(reply)
        if decision is None or decision.get("decision") not in {"pass", "block", "escalate"}:
            # The model replied but produced nothing this lens can act on. That is
            # a real verdict — "I cannot judge this" — so escalate to a human, the
            # conservative call. It is visible (recorded + parks the task), never a
            # silent pass.
            return LensDecision.ESCALATE, f"could not produce a usable verdict: {reply[:160]!r}"
        return LensDecision(str(decision["decision"])), str(decision.get("reason", "")).strip()


def _parse_json_object(reply: str) -> dict | None:
    text = reply.strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
