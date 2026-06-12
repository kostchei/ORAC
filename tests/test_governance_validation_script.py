from __future__ import annotations

from scripts.validate_governance_path import run_validation


def test_governance_validation_script_exercises_dispatch_path(tmp_path) -> None:
    checks = run_validation(tmp_path)

    names = {check.name for check in checks}
    assert {
        "clean dispatch",
        "Intent block",
        "Efficiency block",
        "Optimise escalation",
        "Sentinel escalation",
        "review-after notify",
        "standing-grant cap",
    } <= names
