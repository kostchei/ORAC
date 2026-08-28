from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from orac.adapters import Adapter
from orac.broker_store import BrokerStore, Notification
from orac.code_adapters import code_adapters_for
from orac.models import CapabilityRequest, CapabilityResult, CapabilityStatus, now_iso
from orac.policy import ApprovalMode, approval_mode_for
from orac.storage import BoardStore


@dataclass(frozen=True)
class RollbackResult:
    ok: bool
    message: str
    tool: str | None = None
    data: dict[str, Any] | None = None


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def validate_rollback_contract(contract: dict[str, Any]) -> tuple[bool, str]:
    """Validate that a rollback contract dictionary conforms to version 1 schema."""
    if not isinstance(contract, dict):
        return False, "Rollback contract must be a dict."
    if contract.get("version") != 1:
        return False, f"Unsupported rollback contract version {contract.get('version')!r}; expected 1."
    tool = contract.get("tool")
    if not tool or not isinstance(tool, str):
        return False, "Rollback contract missing valid 'tool' string."
    if not isinstance(contract.get("args"), dict):
        return False, "Rollback contract missing valid 'args' dict."
    if not isinstance(contract.get("expected_state"), dict):
        return False, "Rollback contract missing valid 'expected_state' dict."
    return True, "Valid"


def execute_rollback(
    store: BoardStore,
    notification_id: int,
    *,
    push: bool = False,
    adapters: dict[str, Adapter] | None = None,
    state_checker: Callable[[str, dict[str, Any]], tuple[bool, str]] | None = None,
) -> RollbackResult:
    """Execute a rollback for a durable notification per docs/compensating-actions.md.

    Supports:
    1. Generic non-git compensating contracts (declared in note.data['rollback_contract']).
    2. Git commit reversal (declared in note.data['sha']).

    Fails closed on:
    - Missing or unknown notification
    - Expired contract
    - State drift against expected_state
    - Disallowed / unclassified compensation tools
    - Compensation execution failure
    """
    bstore = BrokerStore(store.root).init()
    try:
        note = bstore.get_notification(notification_id)
    except KeyError:
        return RollbackResult(
            ok=False,
            message=f"Notification [{notification_id}] not found in review queue.",
        )

    root = str(note.data.get("root") or store.root.resolve())

    # Case 1: Generic rollback contract carried in notification data
    contract = note.data.get("rollback_contract")
    if contract:
        valid, reason = validate_rollback_contract(contract)
        if not valid:
            return RollbackResult(
                ok=False,
                message=f"Invalid rollback contract on notification [{notification_id}]: {reason}",
            )

        # Condition 5: Check expiry
        expires_at_str = contract.get("expires_at")
        if expires_at_str:
            expires_at = _parse_iso(expires_at_str)
            if expires_at and datetime.now(timezone.utc) > expires_at:
                return RollbackResult(
                    ok=False,
                    message=(
                        f"Rollback contract on notification [{notification_id}] has expired at "
                        f"{expires_at_str}. Manual reconciliation required."
                    ),
                )

        tool = str(contract["tool"])
        call_args = dict(contract.get("args", {}))
        expected_state = dict(contract.get("expected_state", {}))

        # Condition 4: State drift check
        if state_checker is not None:
            drift_ok, drift_reason = state_checker(tool, expected_state)
            if not drift_ok:
                return RollbackResult(
                    ok=False,
                    message=f"State drift detected for [{notification_id}]: {drift_reason}. Fail closed.",
                )
        else:
            # Built-in media state drift check
            if tool.startswith("media."):
                from orac.media_store import MediaStore  # noqa: PLC0415

                mstore = MediaStore(store.root)
                asset_id = call_args.get("asset_id")
                if asset_id:
                    asset = mstore.get_asset(str(asset_id))
                    if asset is None:
                        return RollbackResult(
                            ok=False,
                            message=f"Asset {asset_id!r} not found in media store. Fail closed.",
                        )
                    if "review_state" in expected_state and asset.review_state != expected_state["review_state"]:
                        return RollbackResult(
                            ok=False,
                            message=(
                                f"Asset {asset_id!r} state drifted from {expected_state['review_state']!r} "
                                f"to {asset.review_state!r}. Manual reconciliation required."
                            ),
                        )

        # Condition 2 & 6: Risk classification & execution
        try:
            mode = approval_mode_for(tool, call_args)
        except ValueError as exc:
            return RollbackResult(
                ok=False,
                message=f"Compensation tool {tool!r} cannot run: {exc}",
            )

        req = CapabilityRequest(
            agent="human",
            tool=tool,
            task_id=note.task_id,
            args=call_args,
        )

        resolved_adapters = adapters
        if resolved_adapters is None or tool not in resolved_adapters:
            # Build adapters for this repo
            from orac.broker import ToolBroker  # noqa: PLC0415

            broker = ToolBroker.from_store(bstore, repo_root=store.root)
            resolved_adapters = broker.adapters

        handler = resolved_adapters.get(tool)
        if handler is None:
            return RollbackResult(
                ok=False,
                message=f"Compensation tool {tool!r} is not registered in adapters.",
            )

        try:
            result = handler(req)
        except Exception as exc:
            bstore.record_audit(
                req,
                CapabilityResult(
                    status=CapabilityStatus.ERROR,
                    tool=tool,
                    message=f"Compensation action {tool!r} failed: {exc}",
                ),
            )
            return RollbackResult(
                ok=False,
                message=f"Compensation action {tool!r} failed: {exc}",
                tool=tool,
            )

        bstore.record_audit(
            req,
            CapabilityResult(
                status=CapabilityStatus.ALLOWED,
                tool=result.name,
                message=result.message,
                data=result.data,
            ),
        )

        if not note.acked:
            bstore.ack_notification(note.id)

        return RollbackResult(
            ok=True,
            message=f"Rolled back [{note.id}] {note.agent} {note.tool} via {tool}: {result.message}",
            tool=tool,
            data=result.data,
        )

    # Case 2: Git revert rollback
    sha = note.data.get("sha")
    if sha:
        code_ad = code_adapters_for((root,)) if adapters is None else adapters
        revert_tool = "git.revert"
        req = CapabilityRequest(
            agent="human",
            tool=revert_tool,
            task_id=note.task_id,
            args={"root": root, "sha": sha},
        )
        revert_handler = code_ad.get(revert_tool)
        if revert_handler is None:
            return RollbackResult(ok=False, message="git.revert adapter not available.")

        try:
            result = revert_handler(req)
        except Exception as exc:
            bstore.record_audit(
                req,
                CapabilityResult(
                    status=CapabilityStatus.ERROR,
                    tool=revert_tool,
                    message=f"git.revert failed: {exc}",
                ),
            )
            return RollbackResult(
                ok=False,
                message=f"git.revert failed: {exc}",
                tool=revert_tool,
            )

        bstore.record_audit(
            req,
            CapabilityResult(
                status=CapabilityStatus.ALLOWED,
                tool=result.name,
                message=result.message,
                data=result.data,
            ),
        )

        if push:
            remote = note.data.get("remote", "origin")
            push_args: dict[str, Any] = {"root": root, "remote": remote}
            branch = note.data.get("branch")
            if branch:
                push_args["branch"] = branch
            push_req = CapabilityRequest(
                agent="human", tool="git.push", task_id=note.task_id, args=push_args
            )
            push_handler = code_ad.get("git.push")
            if push_handler:
                try:
                    push_res = push_handler(push_req)
                    bstore.record_audit(
                        push_req,
                        CapabilityResult(
                            status=CapabilityStatus.ALLOWED,
                            tool=push_res.name,
                            message=push_res.message,
                            data=push_res.data,
                        ),
                    )
                except Exception as exc:
                    bstore.record_audit(
                        push_req,
                        CapabilityResult(
                            status=CapabilityStatus.ERROR,
                            tool="git.push",
                            message=f"git.push failed: {exc}",
                        ),
                    )
                    return RollbackResult(
                        ok=False,
                        message=f"Reverted commit {sha[:8]} locally, but git.push failed: {exc}",
                        tool="git.push",
                    )

        if not note.acked:
            bstore.ack_notification(note.id)

        return RollbackResult(
            ok=True,
            message=f"Rolled back commit {sha[:8]} and acked [{note.id}]: {result.message}",
            tool="git.revert",
            data=result.data,
        )

    return RollbackResult(
        ok=False,
        message=(
            f"Notification [{notification_id}] has no recorded commit sha or rollback contract; "
            "cannot rollback automatically. Reconcile manually, then `orac ack`."
        ),
    )
