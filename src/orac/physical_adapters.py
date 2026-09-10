from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from orac.adapters import Adapter
from orac.models import CapabilityRequest, now_iso
from orac.physical_store import PhysicalDevice, PhysicalStore, PreparedAction
from orac.tooling import ToolResult

PHYSICAL_TOOLS = frozenset(
    {
        "physical.list_entities",
        "physical.read_state",
        "physical.prepare_action",
        "physical.execute_action",
        "physical.emergency_stop",
    }
)


class PhysicalBackend(Protocol):
    def list_entities(self) -> list[dict[str, Any]]: ...

    def read_state(self, entity_id: str) -> dict[str, Any]: ...

    def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class LocalMockHomeAssistantBackend:
    """Mock in-memory Home Assistant backend for test environments and offline operation."""

    def __init__(self) -> None:
        self._entities: dict[str, dict[str, Any]] = {
            "switch.fish_feeder": {
                "entity_id": "switch.fish_feeder",
                "state": "off",
                "attributes": {"friendly_name": "Fish Feeder", "device_class": "switch"},
                "last_updated": now_iso(),
            },
            "light.living_room": {
                "entity_id": "light.living_room",
                "state": "off",
                "attributes": {"friendly_name": "Living Room Light", "brightness": 0},
                "last_updated": now_iso(),
            },
            "climate.thermostat": {
                "entity_id": "climate.thermostat",
                "state": "heat",
                "attributes": {
                    "friendly_name": "Main Thermostat",
                    "temperature": 20.0,
                    "target_temp_high": 24.0,
                    "target_temp_low": 18.0,
                },
                "last_updated": now_iso(),
            },
        }

    def list_entities(self) -> list[dict[str, Any]]:
        return list(self._entities.values())

    def read_state(self, entity_id: str) -> dict[str, Any]:
        if entity_id in self._entities:
            return dict(self._entities[entity_id])
        return {
            "entity_id": entity_id,
            "state": "unknown",
            "attributes": {},
            "last_updated": now_iso(),
        }

    def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = service_data or {}
        current = self._entities.setdefault(
            entity_id,
            {
                "entity_id": entity_id,
                "state": "off",
                "attributes": {},
                "last_updated": now_iso(),
            },
        )

        # Update simulated state based on domain & service
        if service == "turn_on":
            current["state"] = "on"
        elif service == "turn_off":
            current["state"] = "off"
        elif service == "toggle":
            current["state"] = "off" if current.get("state") == "on" else "on"
        elif service == "set_temperature":
            if "temperature" in data:
                current["attributes"]["temperature"] = float(data["temperature"])
        elif service == "feed":
            current["state"] = "feeding"

        # Merge custom attributes
        for k, v in data.items():
            current["attributes"][k] = v
        current["last_updated"] = now_iso()

        return {
            "success": True,
            "entity_id": entity_id,
            "domain": domain,
            "service": service,
            "state": current["state"],
            "attributes": dict(current["attributes"]),
        }


class HomeAssistantRESTBackend:
    """Home Assistant REST API backend."""

    def __init__(
        self,
        base_url: str | None = None,
        access_token: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("HASS_URL") or "http://localhost:8123").rstrip("/")
        self.token = access_token or os.environ.get("HASS_TOKEN") or ""

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def list_entities(self) -> list[dict[str, Any]]:
        req = urllib.request.Request(
            f"{self.base_url}/api/states",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Failed to list Home Assistant entities: {exc}") from exc

    def read_state(self, entity_id: str) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/api/states/{entity_id}",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {
                    "entity_id": entity_id,
                    "state": "unknown",
                    "attributes": {},
                    "last_updated": now_iso(),
                }
            raise RuntimeError(f"Failed to read state for {entity_id!r}: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Failed to read state for {entity_id!r}: {exc}") from exc

    def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {"entity_id": entity_id, **(service_data or {})}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/services/{domain}/{service}",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "domain": domain,
                    "service": service,
                    "response": res_data,
                }
        except Exception as exc:
            raise RuntimeError(f"Failed to call service {domain}.{service}: {exc}") from exc


class PhysicalAdapterSet:
    """Group 4 Physical device control adapters bound to a repository root."""

    def __init__(
        self,
        repo_root: Path | str,
        backend: PhysicalBackend | None = None,
        store: PhysicalStore | None = None,
    ) -> None:
        self.root = Path(repo_root)
        self.store = store or PhysicalStore(self.root)
        self.backend = backend or LocalMockHomeAssistantBackend()

    def adapters(self) -> dict[str, Adapter]:
        return {
            "physical.list_entities": self.physical_list_entities,
            "physical.read_state": self.physical_read_state,
            "physical.prepare_action": self.physical_prepare_action,
            "physical.execute_action": self.physical_execute_action,
            "physical.emergency_stop": self.physical_emergency_stop,
        }

    def physical_list_entities(self, req: CapabilityRequest) -> ToolResult:
        devices = self.store.list_devices()
        results: list[dict[str, Any]] = []
        for dev in devices:
            try:
                state_data = self.backend.read_state(dev.entity_id)
            except Exception as exc:
                state_data = {"error": str(exc), "state": "unavailable"}
            in_cooldown, rem = self.store.is_in_cooldown(dev.id)
            dev_dict = dev.to_dict()
            dev_dict["current_state"] = state_data
            dev_dict["in_cooldown"] = in_cooldown
            dev_dict["cooldown_remaining_seconds"] = rem
            results.append(dev_dict)

        return ToolResult(
            "physical.list_entities",
            f"Found {len(results)} allowlisted physical device(s).",
            {"devices": results},
        )

    def physical_read_state(self, req: CapabilityRequest) -> ToolResult:
        device_id = str(req.args.get("device_id") or "")
        if not device_id:
            raise ValueError("physical.read_state requires 'device_id'.")
        device = self.store.get_device(device_id)
        if not device:
            raise ValueError(f"Device {device_id!r} is not registered or allowlisted.")

        state_data = self.backend.read_state(device.entity_id)
        in_cooldown, rem = self.store.is_in_cooldown(device_id)
        current_val = state_data.get("state", "unknown")

        return ToolResult(
            "physical.read_state",
            f"Device [{device_id}] ({device.entity_id}) state is {current_val!r}.",
            {
                "device_id": device_id,
                "name": device.name,
                "entity_id": device.entity_id,
                "state": state_data,
                "in_cooldown": in_cooldown,
                "cooldown_remaining_seconds": rem,
            },
        )

    def physical_prepare_action(self, req: CapabilityRequest) -> ToolResult:
        device_id = str(req.args.get("device_id") or "")
        if not device_id:
            raise ValueError("physical.prepare_action requires 'device_id'.")
        device = self.store.get_device(device_id)
        if not device:
            raise ValueError(f"Device {device_id!r} is not registered or allowlisted.")

        in_cooldown, rem = self.store.is_in_cooldown(device_id)
        if in_cooldown:
            raise ValueError(
                f"Device {device_id!r} is currently in cooldown ({rem:.1f}s remaining). Action rejected."
            )

        service = str(req.args.get("service") or req.args.get("action") or "")
        if not service:
            raise ValueError("physical.prepare_action requires 'service' (e.g. 'turn_on', 'turn_off').")

        domain = str(
            req.args.get("domain")
            or (device.entity_id.split(".")[0] if "." in device.entity_id else "homeassistant")
        )
        target_state = dict(req.args.get("target_state") or req.args.get("service_data") or {})
        for k, v in req.args.items():
            if k not in ("device_id", "service", "action", "domain", "target_state", "service_data", "ttl_seconds", "task_id"):
                target_state[k] = v

        pre_state = self.backend.read_state(device.entity_id)
        ttl_seconds = int(req.args.get("ttl_seconds", 900))

        prep = self.store.create_prepared_action(
            task_id=req.task_id,
            device_id=device_id,
            service=service,
            domain=domain,
            target_state=target_state,
            pre_state=pre_state,
            ttl_seconds=ttl_seconds,
        )

        self.store.record_action(
            task_id=req.task_id,
            device_id=device_id,
            action_type="prepare",
            details={
                "action_id": prep.id,
                "service": service,
                "domain": domain,
                "target_state": target_state,
                "pre_state": pre_state,
            },
        )

        return ToolResult(
            "physical.prepare_action",
            f"Prepared action [{prep.id}] for device [{device_id}] ({domain}.{service}). Ready for approval/execution.",
            prep.to_dict(),
        )

    def physical_execute_action(self, req: CapabilityRequest) -> ToolResult:
        action_id = str(req.args.get("action_id") or "")
        if not action_id:
            raise ValueError(
                "physical.execute_action requires 'action_id'. Calling execute_action without a live prepare_action record is forbidden."
            )

        prep = self.store.get_prepared_action(action_id)
        if not prep:
            raise ValueError(f"Prepared action {action_id!r} not found.")
        if prep.status != "prepared":
            raise ValueError(f"Prepared action {action_id!r} is in status {prep.status!r}, cannot execute.")
        if prep.is_expired():
            self.store.update_prepared_action_status(action_id, "expired")
            raise ValueError(f"Prepared action {action_id!r} has expired. A fresh prepare_action call is required.")

        device = self.store.get_device(prep.device_id)
        if not device:
            raise ValueError(f"Device {prep.device_id!r} is not in the device allowlist.")

        in_cooldown, rem = self.store.is_in_cooldown(prep.device_id)
        if in_cooldown:
            raise ValueError(
                f"Device {prep.device_id!r} is currently in cooldown ({rem:.1f}s remaining). Execution rejected."
            )

        # Call backend
        try:
            backend_res = self.backend.call_service(
                domain=prep.domain,
                service=prep.service,
                entity_id=device.entity_id,
                service_data=prep.target_state,
            )
        except Exception as exc:
            self.store.record_action(
                task_id=req.task_id,
                device_id=prep.device_id,
                action_type="execute_failed",
                details={"action_id": prep.id, "error": str(exc)},
            )
            raise RuntimeError(f"Physical execution failed on device {device.id!r}: {exc}") from exc

        self.store.update_prepared_action_status(action_id, "executed")
        self.store.record_device_action(prep.device_id)
        self.store.record_action(
            task_id=req.task_id,
            device_id=prep.device_id,
            action_type="execute",
            details={"action_id": prep.id, "backend_result": backend_res},
        )

        post_state = self.backend.read_state(device.entity_id)

        # Build compensating rollback contract if supported
        rollback_contract: dict[str, Any] | None = None
        supports_inverse = bool(device.metadata.get("supports_inverse", False))
        if supports_inverse and prep.pre_state and "state" in prep.pre_state:
            pre_val = prep.pre_state.get("state")
            inv_service: str | None = None
            if prep.service == "turn_on" and pre_val == "off":
                inv_service = "turn_off"
            elif prep.service == "turn_off" and pre_val == "on":
                inv_service = "turn_on"
            elif prep.service == "set_temperature" and "temperature" in prep.pre_state.get("attributes", {}):
                inv_service = "set_temperature"

            if inv_service:
                inv_target = {"temperature": prep.pre_state["attributes"]["temperature"]} if inv_service == "set_temperature" else {}
                restoring_prep = self.store.create_prepared_action(
                    task_id=req.task_id,
                    device_id=device.id,
                    service=inv_service,
                    domain=prep.domain,
                    target_state=inv_target,
                    pre_state=post_state,
                    ttl_seconds=3600,
                )
                rollback_contract = {
                    "version": 1,
                    "tool": "physical.execute_action",
                    "args": {"action_id": restoring_prep.id},
                    "expected_state": {"device_id": device.id, "state": post_state.get("state")},
                    "expires_at": restoring_prep.expires_at,
                    "operator_prompt": f"Restore device {device.id} to pre-action state {pre_val!r} via {inv_service}?",
                }

        return ToolResult(
            "physical.execute_action",
            f"Executed action [{prep.id}] on device [{device.id}] ({prep.domain}.{prep.service}). Result state: {post_state.get('state')!r}.",
            {
                "action_id": prep.id,
                "device_id": device.id,
                "entity_id": device.entity_id,
                "status": "executed",
                "backend_result": backend_res,
                "post_state": post_state,
                "rollback_contract": rollback_contract,
            },
        )

    def physical_emergency_stop(self, req: CapabilityRequest) -> ToolResult:
        device_id = req.args.get("device_id")
        stopped: list[str] = []

        if device_id:
            dev = self.store.get_device(str(device_id))
            if not dev:
                raise ValueError(f"Device {device_id!r} is not in the device allowlist.")
            domain = dev.entity_id.split(".")[0] if "." in dev.entity_id else "homeassistant"
            self.backend.call_service(domain=domain, service="turn_off", entity_id=dev.entity_id)
            self.store.record_action(
                task_id=req.task_id,
                device_id=dev.id,
                action_type="emergency_stop",
                details={"entity_id": dev.entity_id},
            )
            stopped.append(dev.id)
            msg = f"Emergency stop executed for device [{dev.id}]."
        else:
            devices = self.store.list_devices()
            for dev in devices:
                domain = dev.entity_id.split(".")[0] if "." in dev.entity_id else "homeassistant"
                try:
                    self.backend.call_service(domain=domain, service="turn_off", entity_id=dev.entity_id)
                    self.store.record_action(
                        task_id=req.task_id,
                        device_id=dev.id,
                        action_type="emergency_stop",
                        details={"entity_id": dev.entity_id},
                    )
                    stopped.append(dev.id)
                except Exception:
                    pass
            msg = f"Global emergency stop executed across {len(stopped)} allowlisted device(s)."

        return ToolResult(
            "physical.emergency_stop",
            msg,
            {"devices_stopped": stopped, "status": "stopped"},
        )


def physical_adapters_for(
    repo_root: Path | str,
    backend: PhysicalBackend | None = None,
    store: PhysicalStore | None = None,
) -> dict[str, Adapter]:
    return PhysicalAdapterSet(repo_root, backend=backend, store=store).adapters()
