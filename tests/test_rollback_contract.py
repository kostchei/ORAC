from __future__ import annotations

import pytest

from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.models import CapabilityRequest, CapabilityStatus, Task, TaskStatus
from orac.rollback_contract import (
    RollbackContractError,
    apply_rollback,
    fs_write_contract,
    validate_contract,
)


# --- the contract + schema validation ---------------------------------------

def test_fs_write_contract_validates_against_the_schema() -> None:
    contract = fs_write_contract("/tmp/x.txt", existed=False, content_before=None)
    validate_contract(contract)  # does not raise
    assert contract["inverse_operation"]["operation"] == "fs.restore_file"
    assert "idempotency_key" in contract


def test_validate_contract_rejects_missing_required_field() -> None:
    with pytest.raises(RollbackContractError, match="missing required field"):
        validate_contract({"target_resource": "x", "idempotency_key": "k"})  # no inverse


def test_apply_rollback_restores_prior_content(tmp_path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("ORIGINAL", encoding="utf-8")
    contract = fs_write_contract(str(target), existed=True, content_before="ORIGINAL")
    target.write_text("MUTATED", encoding="utf-8")  # the action changed it

    apply_rollback(contract)

    assert target.read_text(encoding="utf-8") == "ORIGINAL"


def test_apply_rollback_deletes_a_file_that_did_not_exist(tmp_path) -> None:
    target = tmp_path / "new.txt"
    contract = fs_write_contract(str(target), existed=False, content_before=None)
    target.write_text("created by the action", encoding="utf-8")

    apply_rollback(contract)

    assert not target.exists()


def test_apply_rollback_unknown_operation_raises_for_human_in_the_loop() -> None:
    contract = {
        "target_resource": "device:heater",
        "idempotency_key": "k",
        "inverse_operation": {"operation": "device.power_off"},  # no auto handler
    }
    with pytest.raises(RollbackContractError, match="manual undo required"):
        apply_rollback(contract)


# --- end-to-end through the broker + review queue ---------------------------

def test_external_write_lands_in_queue_with_a_contract(tmp_path) -> None:
    (tmp_path / ".orac").mkdir()
    store = BrokerStore(tmp_path).init()
    broker = ToolBroker.from_store(store, repo_root=tmp_path)
    task = Task(title="emit artifact", status=TaskStatus.IN_PROGRESS)

    result = broker.request(
        CapabilityRequest(
            agent="Builder", tool="fs.write_external_file", task_id=task.id,
            args={"path": "report.txt", "content": "hello"},
        ),
        task,
    )

    assert result.status is CapabilityStatus.ALLOWED          # notify: runs immediately
    out = tmp_path / ".orac" / "outputs" / "report.txt"
    assert out.read_text(encoding="utf-8") == "hello"

    # It is recorded in the review-after queue, carrying its rollback contract.
    notes = store.list_notifications(unacked_only=True)
    assert len(notes) == 1
    contract = notes[0].data["rollback_contract"]
    validate_contract(contract)

    # And the contract genuinely rolls the write back (file didn't exist before).
    apply_rollback(contract)
    assert not out.exists()
