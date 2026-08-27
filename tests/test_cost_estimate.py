from __future__ import annotations

import json
from pathlib import Path

import pytest

from orac.cost_estimate import estimate_goal
from orac.storage import BoardStore


def test_estimate_ranges_never_collapse_to_points(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    store.init()

    est = estimate_goal("Implement new unit test for broker dispatch", root=tmp_path)
    t_low, t_high = est.tokens_range
    s_low, s_high = est.time_seconds_range

    assert t_low < t_high
    assert s_low < s_high
    assert est.goal == "Implement new unit test for broker dispatch"
    assert "heuristic" in est.confidence


def test_estimate_refuses_dollar_cost_when_unmeasured(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    store.init()
    # Empty usage.json (default)
    est = estimate_goal("Refactor council lenses", root=tmp_path)
    assert est.cost_usd_range is None
    assert est.grounded is False
    assert "None (unmeasured" in est.format_cli()


def test_estimate_populates_dollar_cost_when_rate_measured(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    store.init()

    # Populate measured rate in usage.json
    usage_path = tmp_path / ".orac" / "usage.json"
    usage_path.write_text(
        json.dumps({
            "foundation": {"2026-08-27": 0.25},
            "measured_rate_usd_per_ktok": 0.0025,
        }),
        encoding="utf-8",
    )

    est = estimate_goal("Complex multi-agent task with deep tree decomposition", root=tmp_path)
    assert est.cost_usd_range is not None
    assert est.grounded is True

    c_low, c_high = est.cost_usd_range
    assert c_low < c_high
    assert c_low > 0
    cli_out = est.format_cli()
    assert "$" in cli_out
    assert "grounded on measured spend" in cli_out


def test_estimate_scales_with_goal_length(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    store.init()

    short_est = estimate_goal("Fix typo", root=tmp_path)
    long_goal = (
        "Architect and implement a high-throughput async event streaming pipeline with "
        "backpressure, sliding window rate limiting, dead-letter queues, and end-to-end "
        "distributed tracing across microservices."
    )
    long_est = estimate_goal(long_goal, root=tmp_path)

    assert short_est.tokens_range[1] < long_est.tokens_range[1]
    assert short_est.time_seconds_range[1] < long_est.time_seconds_range[1]
