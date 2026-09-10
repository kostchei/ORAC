from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orac.agent_registry import load_agent_profiles
from orac.broker import ToolBroker
from orac.broker_store import BrokerStore, Notification
from orac.compensating import execute_rollback
from orac.models import CapabilityRequest, CapabilityResult, CapabilityStatus, Task, TaskStatus
from orac.physical_adapters import LocalMockHomeAssistantBackend, PhysicalAdapterSet, physical_adapters_for
from orac.physical_store import PhysicalStore
from orac.policy import ApprovalMode, approval_mode_for
from orac.storage import BoardStore
from orac.work import WORK_KINDS, verify_goal_done


def test_operator_agent_registered() -> None:
    profiles = {p.slug: p for p in load_agent_profiles()}
    assert "operator" in profiles
    op = profiles["operator"]
    assert op.kind == "doer"
    assert "physical.list_entities" in op.tools
    assert "physical.read_state" in op.tools
    assert "physical.prepare_action" in op.tools
    assert "physical.execute_action" in op.tools
    assert "physical.emergency_stop" in op.tools
    assert "skill.list" in op.tools
    assert "skill.view" in op.tools

    # Operator is the sole holder of physical.execute_action
    for slug, profile in profiles.items():
        if slug != "operator":
            assert "physical.execute_action" not in profile.tools


def test_physical_risk_classification() -> None:
    assert approval_mode_for("physical.list_entities") is ApprovalMode.AUTO
    assert approval_mode_for("physical.read_state") is ApprovalMode.AUTO
    assert approval_mode_for("physical.prepare_action") is ApprovalMode.NOTIFY
    assert approval_mode_for("physical.execute_action") is ApprovalMode.APPROVE
    assert approval_mode_for("physical.emergency_stop") is ApprovalMode.AUTO


def test_physical_store_lifecycle(tmp_path: Path) -> None:
    store = PhysicalStore(tmp_path)

    # 1. Default devices seeded
    devices = store.list_devices()
    assert len(devices) >= 3
    feeder = store.get_device("feeder_1")
    assert feeder is not None
    assert feeder.entity_id == "switch.fish_feeder"
    assert feeder.cooldown_seconds == 300

    # 2. Register custom device
    fan = store.register_device(
        device_id="fan_master",
        name="Master Bedroom Fan",
        entity_id="fan.master_bedroom",
        backend="mock",
        cooldown_seconds=10,
        metadata={"speed_steps": 3},
    )
    assert fan.id == "fan_master"
    assert store.get_device("fan_master") is not None

    # 3. Cooldown check
    in_cd, rem = store.is_in_cooldown("fan_master")
    assert not in_cd
    assert rem == 0.0

    store.record_device_action("fan_master")
    in_cd2, rem2 = store.is_in_cooldown("fan_master")
    assert in_cd2
    assert rem2 > 0.0

    # 4. Prepared action
    prep = store.create_prepared_action(
        task_id="task-100",
        device_id="fan_master",
        service="turn_on",
        domain="fan",
        target_state={"speed": 2},
        pre_state={"state": "off"},
        ttl_seconds=60,
    )
    assert prep.id.startswith("prep-")
    assert prep.status == "prepared"
    assert not prep.is_expired()

    fetched_prep = store.get_prepared_action(prep.id)
    assert fetched_prep is not None
    assert fetched_prep.service == "turn_on"
    assert fetched_prep.target_state == {"speed": 2}

    # 5. Update prepared action status
    store.update_prepared_action_status(prep.id, "executed")
    assert store.get_prepared_action(prep.id).status == "executed"

    # 6. Action audit logging
    act = store.record_action(
        task_id="task-100",
        device_id="fan_master",
        action_type="execute",
        details={"result": "ok"},
    )
    assert act.id.startswith("pact-")
    actions = store.list_actions(task_id="task-100")
    assert len(actions) == 1
    assert actions[0].action_type == "execute"


def test_physical_adapters_full_flow(tmp_path: Path) -> None:
    pset = PhysicalAdapterSet(tmp_path)
    adapters = pset.adapters()

    # 1. List entities (allowlisted)
    list_res = adapters["physical.list_entities"](
        CapabilityRequest(agent="Operator", tool="physical.list_entities", task_id="t-1", args={})
    )
    assert list_res.name == "physical.list_entities"
    assert len(list_res.data["devices"]) >= 3

    # 2. Read state of living_room_light
    read_res = adapters["physical.read_state"](
        CapabilityRequest(
            agent="Operator",
            tool="physical.read_state",
            task_id="t-1",
            args={"device_id": "living_room_light"},
        )
    )
    assert read_res.name == "physical.read_state"
    assert read_res.data["state"]["state"] == "off"

    # 3. Prepare action
    prep_res = adapters["physical.prepare_action"](
        CapabilityRequest(
            agent="Operator",
            tool="physical.prepare_action",
            task_id="t-1",
            args={"device_id": "living_room_light", "service": "turn_on"},
        )
    )
    assert prep_res.name == "physical.prepare_action"
    action_id = prep_res.data["id"]
    assert action_id.startswith("prep-")
    assert prep_res.data["pre_state"]["state"] == "off"

    # 4. Execute action
    exec_res = adapters["physical.execute_action"](
        CapabilityRequest(
            agent="Operator",
            tool="physical.execute_action",
            task_id="t-1",
            args={"action_id": action_id},
        )
    )
    assert exec_res.name == "physical.execute_action"
    assert exec_res.data["status"] == "executed"
    assert exec_res.data["post_state"]["state"] == "on"
    assert "rollback_contract" in exec_res.data
    contract = exec_res.data["rollback_contract"]
    assert contract is not None
    assert contract["tool"] == "physical.execute_action"

    # 5. Read state confirms update
    read_res2 = adapters["physical.read_state"](
        CapabilityRequest(
            agent="Operator",
            tool="physical.read_state",
            task_id="t-1",
            args={"device_id": "living_room_light"},
        )
    )
    assert read_res2.data["state"]["state"] == "on"

    # 6. Immediate second prepare_action rejected due to cooldown
    with pytest.raises(ValueError, match="cooldown"):
        adapters["physical.prepare_action"](
            CapabilityRequest(
                agent="Operator",
                tool="physical.prepare_action",
                task_id="t-1",
                args={"device_id": "living_room_light", "service": "turn_off"},
            )
        )


def test_physical_unprepared_execute_denied(tmp_path: Path) -> None:
    pset = PhysicalAdapterSet(tmp_path)
    adapters = pset.adapters()

    with pytest.raises(ValueError, match="action_id"):
        adapters["physical.execute_action"](
            CapabilityRequest(
                agent="Operator",
                tool="physical.execute_action",
                task_id="t-1",
                args={},
            )
        )

    with pytest.raises(ValueError, match="not found"):
        adapters["physical.execute_action"](
            CapabilityRequest(
                agent="Operator",
                tool="physical.execute_action",
                task_id="t-1",
                args={"action_id": "prep-nonexistent"},
            )
        )


def test_physical_non_allowlisted_device_denied(tmp_path: Path) -> None:
    pset = PhysicalAdapterSet(tmp_path)
    adapters = pset.adapters()

    with pytest.raises(ValueError, match="not registered or allowlisted"):
        adapters["physical.read_state"](
            CapabilityRequest(
                agent="Operator",
                tool="physical.read_state",
                task_id="t-1",
                args={"device_id": "unknown_smart_meter"},
            )
        )

    with pytest.raises(ValueError, match="not registered or allowlisted"):
        adapters["physical.prepare_action"](
            CapabilityRequest(
                agent="Operator",
                tool="physical.prepare_action",
                task_id="t-1",
                args={"device_id": "unknown_smart_meter", "service": "turn_on"},
            )
        )


def test_physical_emergency_stop_bypass(tmp_path: Path) -> None:
    backend = LocalMockHomeAssistantBackend()
    pset = PhysicalAdapterSet(tmp_path, backend=backend)
    adapters = pset.adapters()

    # Turn on light directly in backend
    backend.call_service("light", "turn_on", "light.living_room")
    assert backend.read_state("light.living_room")["state"] == "on"

    # Emergency stop targets living_room_light without prepare step
    estop_res = adapters["physical.emergency_stop"](
        CapabilityRequest(
            agent="Operator",
            tool="physical.emergency_stop",
            task_id="t-1",
            args={"device_id": "living_room_light"},
        )
    )
    assert estop_res.name == "physical.emergency_stop"
    assert backend.read_state("light.living_room")["state"] == "off"

    # Global emergency stop
    backend.call_service("switch", "turn_on", "switch.fish_feeder")
    backend.call_service("light", "turn_on", "light.living_room")
    global_stop = adapters["physical.emergency_stop"](
        CapabilityRequest(
            agent="Operator",
            tool="physical.emergency_stop",
            task_id="t-1",
            args={},
        )
    )
    assert len(global_stop.data["devices_stopped"]) >= 3
    assert backend.read_state("switch.fish_feeder")["state"] == "off"
    assert backend.read_state("light.living_room")["state"] == "off"


def test_physical_rollback_and_state_drift(tmp_path: Path) -> None:
    bstore = BrokerStore(tmp_path).init()
    pstore = PhysicalStore(tmp_path)
    backend = LocalMockHomeAssistantBackend()
    pset = PhysicalAdapterSet(tmp_path, backend=backend, store=pstore)
    adapters = pset.adapters()

    # 1. Prepare and execute action
    prep_res = adapters["physical.prepare_action"](
        CapabilityRequest(
            agent="Operator",
            tool="physical.prepare_action",
            task_id="t-1",
            args={"device_id": "living_room_light", "service": "turn_on"},
        )
    )
    exec_res = adapters["physical.execute_action"](
        CapabilityRequest(
            agent="Operator",
            tool="physical.execute_action",
            task_id="t-1",
            args={"action_id": prep_res.data["id"]},
        )
    )
    assert backend.read_state("light.living_room")["state"] == "on"
    contract = exec_res.data["rollback_contract"]
    assert contract is not None

    # 2. Record notification carrying rollback contract
    note_id = bstore.record_notification(
        CapabilityRequest(
            agent="Operator",
            tool="physical.execute_action",
            task_id="t-1",
            args={"action_id": prep_res.data["id"]},
        ),
        CapabilityResult(
            status=CapabilityStatus.ALLOWED,
            tool="physical.execute_action",
            message="executed",
            data={
                "root": str(tmp_path.resolve()),
                "rollback_contract": contract,
            },
        ),
    )

    board_store = BoardStore(tmp_path)
    board_store.init()

    # 3. Simulate state drift before rollback: device turned off externally
    backend.call_service("light", "turn_off", "light.living_room")
    assert backend.read_state("light.living_room")["state"] == "off"

    # Drift detection triggers fail-closed
    res_drift = execute_rollback(board_store, note_id, adapters=adapters)
    assert not res_drift.ok
    assert "state drifted" in res_drift.message.lower()

    # 4. Restore expected state ("on") and reset cooldown so compensation succeeds
    backend.call_service("light", "turn_on", "light.living_room")
    pstore.record_device_action("living_room_light", action_time="2020-01-01T00:00:00+00:00")

    res_rollback = execute_rollback(board_store, note_id, adapters=adapters)
    assert res_rollback.ok
    assert backend.read_state("light.living_room")["state"] == "off"


def test_physical_work_kind_and_verifier(tmp_path: Path) -> None:
    bstore = BrokerStore(tmp_path).init()
    broker = ToolBroker.from_store(bstore, repo_root=tmp_path)
    spec = WORK_KINDS["physical"]
    assert spec.doer_slug == "operator"
    assert spec.verifiers == ("verify_physical_action",)

    child = Task(id="phys-task-1", title="Feed the fish", status=TaskStatus.IN_PROGRESS)

    # Initially no physical action in store => verifier fails
    ok, msg = verify_goal_done(spec, child, broker, {"repo_root": tmp_path})
    assert not ok
    assert "no executed physical action" in msg

    # Record executed physical action in physical store
    pstore = PhysicalStore(tmp_path)
    pstore.record_action(task_id=child.id, device_id="feeder_1", action_type="execute")

    # Now verifier passes
    ok, msg = verify_goal_done(spec, child, broker, {"repo_root": tmp_path})
    assert ok
    assert "physical action" in msg
