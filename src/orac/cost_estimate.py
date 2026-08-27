from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orac.model_policy import ModelPolicyStore
from orac.storage import BoardStore


@dataclass(frozen=True)
class CostEstimate:
    """Prospective token, latency, and cost estimates for a goal before execution.

    W1: Read-only (never mutates board or broker state).
    W2: Advisory-only (informational estimate, never blocks/gates).
    W3: Honesty discipline (ranges only, refuses to fabricate ungrounded dollar figures).
    """

    goal: str
    tokens_range: tuple[int, int]
    time_seconds_range: tuple[int, int]
    cost_usd_range: tuple[float, float] | None
    confidence: str
    grounded: bool

    def format_cli(self) -> str:
        t_low, t_high = self.tokens_range
        s_low, s_high = self.time_seconds_range
        lines = [
            f"Prospective estimate for goal: {self.goal!r}",
            f"  Tokens:     {t_low:,} – {t_high:,}",
            f"  Time:       {s_low}s – {s_high}s",
        ]
        if self.cost_usd_range is not None and self.grounded:
            c_low, c_high = self.cost_usd_range
            lines.append(f"  Est. Cost:  ${c_low:.4f} – ${c_high:.4f} USD (grounded on measured spend)")
        else:
            lines.append("  Est. Cost:  None (unmeasured / local / free browser brain)")
        lines.append(f"  Confidence: {self.confidence}")
        return "\n".join(lines)


def _compute_token_range(goal_text: str) -> tuple[int, int]:
    """Derive token heuristic range based on goal complexity."""
    words = len(goal_text.strip().split())
    if words < 10:
        return (1000, 4000)
    elif words < 30:
        return (2000, 7500)
    else:
        return (3500, 12000)


def _compute_time_range(tokens_range: tuple[int, int]) -> tuple[int, int]:
    """Derive expected execution latency in seconds."""
    low_tokens, high_tokens = tokens_range
    # Approximate ~50-80 tokens/sec execution speed plus tool latency
    time_low = max(5, int(low_tokens / 120) + 5)
    time_high = max(time_low + 10, int(high_tokens / 60) + 15)
    return (time_low, time_high)


def _get_measured_usd_per_1ktok(store: BoardStore) -> float | None:
    """Retrieve measured foundation spend rate from usage.json if present."""
    policy_store = ModelPolicyStore(store)
    usage = policy_store.usage()

    # Explicit rate if configured/measured
    if "measured_rate_usd_per_ktok" in usage:
        return float(usage["measured_rate_usd_per_ktok"])

    foundation = usage.get("foundation", {})
    if not isinstance(foundation, dict):
        return None

    # Check for historical spend records
    total_spent = sum(float(v) for v in foundation.values() if isinstance(v, (int, float)))
    if total_spent > 0 and "measured_tokens" in usage and usage["measured_tokens"] > 0:
        return total_spent / (usage["measured_tokens"] / 1000)

    # Spend without its measured token denominator is not a rate. Refuse to
    # convert it into dollars-per-token rather than smuggling in a price guess.
    return None


def estimate_goal(goal_text: str, root: Path | str = ".") -> CostEstimate:
    """Produce bounded prospective token, latency, and cost estimates for a goal."""
    tokens = _compute_token_range(goal_text)
    time_range = _compute_time_range(tokens)
    store = BoardStore(Path(root))
    rate = _get_measured_usd_per_1ktok(store)

    if rate is not None and rate > 0:
        cost_low = round((tokens[0] / 1000.0) * rate, 4)
        cost_high = round((tokens[1] / 1000.0) * rate, 4)
        cost_range = (cost_low, cost_high)
        grounded = True
    else:
        cost_range = None
        grounded = False

    return CostEstimate(
        goal=goal_text,
        tokens_range=tokens,
        time_seconds_range=time_range,
        cost_usd_range=cost_range,
        confidence="medium (bounded heuristic range)",
        grounded=grounded,
    )
