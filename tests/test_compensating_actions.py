from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orac.adapters import Adapter
from orac.broker_store import BrokerStore
from orac.compensating import execute_rollback, validate_rollback_contract
from orac.models import CapabilityRequest, CapabilityResult, CapabilityStatus
from orac.storage import BoardStore
from orac.tooling import ToolResult


def test_validate_rollback_contract_schema() -> None:
    # Valid contract
    contract = {
        "version": 1,
        "tool": "media.archive_asset",
        "args": {"asset_id": "asset-123"},
        "expected_state": {"review_state": "generated"},
        "expires_at": None,
        "operator_prompt": "Archive generated asset asset-123?",
    }
    ok, msg = validate_rollback_contract(contract)
    assert ok, msg

    # Invalid version
    ok, msg = validate_rollback_contract({**contract, "version": 2})
    assert not ok
    assert "expected 1" in msg

    # Missing tool
    ok, msg = validate_rollback_contract({**contract, "tool": ""})
    assert not ok


def test_execute_rollback_expired_fails_closed(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    store.init()
    bstore = BrokerStore(tmp_path).init()

    note_id = bstore.record_notification(
        CapabilityRequest(agent="Producer", tool="comfy.generate_image", task_id="t-1", args={}),
        CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool="comfy.generate_image",
            message="generated",
            data={
                "rollback_contract": {
                    "version": 1,
                    "tool": "media.archive_asset",
                    "args": {"asset_id": "asset-123"},
                    "expected_state": {"review_state": "generated"},
                    "expires_at": "2020-01-01T00:00:00+00:00",  # past
                }
            },
        ),
    )

    res = execute_rollback(store, note_id)
    assert not res.ok
    assert "expired" in res.message


def test_execute_rollback_state_drift_fails_closed(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    store.init()
    bstore = BrokerStore(tmp_path).init()

    note_id = bstore.record_notification(
        CapabilityRequest(agent="Producer", tool="comfy.generate_image", task_id="t-1", args={}),
        CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool="comfy.generate_image",
            message="generated",
            data={
                "rollback_contract": {
                    "version": 1,
                    "tool": "media.archive_asset",
                    "args": {"asset_id": "asset-123"},
                    "expected_state": {"review_state": "generated"},
                    "expires_at": None,
                }
            },
        ),
    )

    # Injected state checker reports drift
    def drifting_state_checker(tool: str, expected_state: dict[str, Any]) -> tuple[bool, str]:
        return False, "current review_state is 'published', not 'generated'"

    res = execute_rollback(store, note_id, state_checker=drifting_state_checker)
    assert not res.ok
    assert "State drift detected" in res.message


def test_execute_rollback_success_acks_and_audits(tmp_path: Path) -> None:
    store = BoardStore(tmp_path)
    store.init()
    bstore = BrokerStore(tmp_path).init()

    note_id = bstore.record_notification(
        CapabilityRequest(agent="Producer", tool="comfy.generate_image", task_id="t-1", args={}),
        CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool="comfy.generate_image",
            message="generated",
            data={
                "rollback_contract": {
                    "version": 1,
                    "tool": "media.archive_asset",
                    "args": {"asset_id": "asset-123"},
                    "expected_state": {"review_state": "generated"},
                    "expires_at": None,
                }
            },
        ),
    )

    # Mock adapter
    archive_called = []

    def mock_archive_adapter(req: CapabilityRequest) -> ToolResult:
        archive_called.append(req)
        return ToolResult("media.archive_asset", "Asset asset-123 archived.", {})

    adapters = {"media.archive_asset": mock_archive_adapter}

    res = execute_rollback(
        store,
        note_id,
        adapters=adapters,
        state_checker=lambda tool, state: (True, "ok"),
    )
    assert res.ok
    assert len(archive_called) == 1
    assert archive_called[0].agent == "human"

    # Check notification is acknowledged
    note = bstore.get_notification(note_id)
    assert note.acked

    # Check audit log contains human action
    human_audits = bstore.human_audit_log()
    assert any(a.tool == "media.archive_asset" for a in human_audits)
