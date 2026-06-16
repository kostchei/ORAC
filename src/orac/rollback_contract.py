from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

# Non-git rollback contracts (the framework for "external actions" rollback).
#
# git-reversible actions roll back with git.revert (a recorded commit sha). But a
# tool that mutates state git cannot see — a sent message, a toggled device, a file
# written outside the repo — has no sha to revert. Such a tool must instead record a
# RollbackContract describing how to undo itself, which lands in the review-after
# queue alongside the action. `orac rollback` then either applies the contract's
# inverse automatically, or, when the inverse is not automatable, hands the operator
# explicit manual steps (the human-in-the-loop path).
#
# The contract shape is defined once in schemas/rollback_contract.json and validated
# against it at runtime here — the schema is the source of truth, not prose.

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "rollback_contract.json"


class RollbackContractError(ValueError):
    """A rollback contract is malformed or its inverse cannot be applied."""


def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_contract(contract: dict[str, Any]) -> None:
    """Validate a contract against schemas/rollback_contract.json. Fail closed:
    a missing required field or a malformed inverse raises, never passes silently.

    A deliberately small, dependency-free check driven by the schema's own
    ``required`` lists (so the JSON schema genuinely governs validation) rather
    than a re-statement of the rules in code."""
    schema = _schema()
    if not isinstance(contract, dict):
        raise RollbackContractError("rollback contract must be an object.")
    for field in schema.get("required", []):
        if field not in contract:
            raise RollbackContractError(f"rollback contract missing required field {field!r}.")
    inverse = contract["inverse_operation"]
    inverse_required = schema["properties"]["inverse_operation"].get("required", [])
    if not isinstance(inverse, dict) or any(f not in inverse for f in inverse_required):
        raise RollbackContractError(
            f"inverse_operation must be an object with {inverse_required}."
        )


def fs_write_contract(path: str, existed: bool, content_before: str | None) -> dict[str, Any]:
    """Build the rollback contract for a write to an external (non-repo) file.

    Captures the FullRollbackPayload form: the inverse restores the prior content,
    or deletes the file if it did not exist before the write."""
    contract = {
        "target_resource": str(Path(path).resolve()),
        "idempotency_key": uuid.uuid4().hex,
        "inverse_operation": {
            "operation": "fs.restore_file",
            "state_before": {
                "path": str(Path(path).resolve()),
                "existed": existed,
                "content": content_before,
            },
        },
    }
    validate_contract(contract)
    return contract


def apply_rollback(contract: dict[str, Any]) -> str:
    """Apply a contract's inverse operation. Returns a human-readable result.

    Raises RollbackContractError when the operation is unknown or unsafe — the
    dispatcher turns that into the human-in-the-loop path rather than guessing.
    """
    validate_contract(contract)
    inverse = contract["inverse_operation"]
    operation = inverse["operation"]
    if operation == "fs.restore_file":
        state = inverse.get("state_before") or {}
        path = Path(state["path"])
        if state.get("existed"):
            path.write_text(state.get("content") or "", encoding="utf-8")
            return f"Restored {path} to its prior contents."
        if path.exists():
            path.unlink()
        return f"Removed {path} (it did not exist before the action)."
    raise RollbackContractError(
        f"no automatic rollback for inverse operation {operation!r}; manual undo required."
    )
