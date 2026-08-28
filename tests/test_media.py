from __future__ import annotations

from pathlib import Path

import pytest

from orac.agent_registry import load_agent_profiles
from orac.broker import ToolBroker
from orac.broker_store import BrokerStore
from orac.media_adapters import MediaAdapterSet, media_adapters_for
from orac.media_store import MediaStore
from orac.models import CapabilityRequest, CapabilityStatus, Task, TaskStatus
from orac.policy import ApprovalMode, approval_mode_for, risk_class
from orac.work import WORK_KINDS, verify_goal_done


def test_producer_agent_registered() -> None:
    profiles = {p.slug: p for p in load_agent_profiles()}
    assert "producer" in profiles
    prod = profiles["producer"]
    assert prod.kind == "doer"
    assert "comfy.workflow_list" in prod.tools
    assert "comfy.generate_image" in prod.tools
    assert "comfy.queue_status" in prod.tools
    assert "comfy.fetch_artifact" in prod.tools
    assert "media.review_asset" in prod.tools
    assert "media.publish_asset" in prod.tools
    assert "media.archive_asset" in prod.tools


def test_media_risk_classification() -> None:
    assert approval_mode_for("comfy.workflow_list") is ApprovalMode.AUTO
    assert approval_mode_for("comfy.generate_image") is ApprovalMode.AUTO
    assert approval_mode_for("comfy.queue_status") is ApprovalMode.AUTO
    assert approval_mode_for("comfy.fetch_artifact") is ApprovalMode.AUTO
    assert approval_mode_for("media.review_asset") is ApprovalMode.AUTO
    assert approval_mode_for("media.archive_asset") is ApprovalMode.AUTO
    # External public publish is approve-gated
    assert approval_mode_for("media.publish_asset") is ApprovalMode.APPROVE


def test_media_store_lifecycle(tmp_path: Path) -> None:
    store = MediaStore(tmp_path)

    # 1. Create job
    job = store.create_job(task_id="task-1", workflow="txt2img_sdxl", prompt="a glowing orb")
    assert job.id.startswith("media-job-")
    assert job.status == "queued"

    # 2. Update job
    store.update_job_status(job.id, status="completed", result={"output": "done"})
    updated_job = store.get_job(job.id)
    assert updated_job is not None
    assert updated_job.status == "completed"

    # 3. Create asset
    asset = store.create_asset(
        task_id="task-1",
        name="test_orb",
        content=b"dummy image bytes",
        mime_type="image/png",
        job_id=job.id,
    )
    assert asset.id.startswith("asset-")
    assert asset.review_state == "generated"
    assert Path(asset.path).exists()

    # 4. Review asset
    reviewed = store.update_asset_state(asset.id, review_state="reviewed", notes="Looks great")
    assert reviewed is not None
    assert reviewed.review_state == "reviewed"
    assert reviewed.metadata.get("review_notes") == "Looks great"

    # 5. Archive asset (rollback)
    archived = store.archive_asset(asset.id)
    assert archived is not None
    assert archived.review_state == "archived"
    assert Path(archived.path).exists()
    assert "archive" in archived.path


def test_media_adapters_full_flow(tmp_path: Path) -> None:
    mset = MediaAdapterSet(tmp_path)
    adapters = mset.adapters()

    # 1. List workflows
    wf_res = adapters["comfy.workflow_list"](
        CapabilityRequest(agent="Producer", tool="comfy.workflow_list", task_id="t-1", args={})
    )
    assert wf_res.name == "comfy.workflow_list"
    assert len(wf_res.data["workflows"]) >= 1

    # 2. Generate image (queued)
    gen_res = adapters["comfy.generate_image"](
        CapabilityRequest(
            agent="Producer",
            tool="comfy.generate_image",
            task_id="t-1",
            args={"workflow": "txt2img_sdxl", "prompt": "cyberpunk console"},
        )
    )
    assert gen_res.name == "comfy.generate_image"
    job_id = gen_res.data["job_id"]
    assert job_id

    # 3. Queue status
    status_res = adapters["comfy.queue_status"](
        CapabilityRequest(
            agent="Producer",
            tool="comfy.queue_status",
            task_id="t-1",
            args={"job_id": job_id},
        )
    )
    assert status_res.name == "comfy.queue_status"
    assert status_res.data["status"] == "completed"

    # 4. Fetch artifact
    fetch_res = adapters["comfy.fetch_artifact"](
        CapabilityRequest(
            agent="Producer",
            tool="comfy.fetch_artifact",
            task_id="t-1",
            args={"job_id": job_id, "name": "cyberpunk_console"},
        )
    )
    assert fetch_res.name == "comfy.fetch_artifact"
    asset_id = fetch_res.data["asset_id"]
    assert fetch_res.data["review_state"] == "generated"
    assert "rollback_contract" in fetch_res.data
    contract = fetch_res.data["rollback_contract"]
    assert contract["tool"] == "media.archive_asset"
    assert contract["args"]["asset_id"] == asset_id

    # 5. Review asset
    review_res = adapters["media.review_asset"](
        CapabilityRequest(
            agent="Producer",
            tool="media.review_asset",
            task_id="t-1",
            args={"asset_id": asset_id, "notes": "Approved by producer"},
        )
    )
    assert review_res.name == "media.review_asset"
    assert review_res.data["review_state"] == "reviewed"

    # 6. Publish asset
    pub_res = adapters["media.publish_asset"](
        CapabilityRequest(
            agent="Producer",
            tool="media.publish_asset",
            task_id="t-1",
            args={"asset_id": asset_id, "destination": "outputs/published"},
        )
    )
    assert pub_res.name == "media.publish_asset"
    assert pub_res.data["review_state"] == "published"
    assert Path(pub_res.data["published_path"]).exists()


def test_media_work_kind_and_verifier(tmp_path: Path) -> None:
    bstore = BrokerStore(tmp_path).init()
    broker = ToolBroker.from_store(bstore, repo_root=tmp_path)
    spec = WORK_KINDS["media"]
    assert spec.doer_slug == "producer"
    assert spec.verifiers == ("verify_media_artifact",)

    child = Task(id="media-task-1", title="Generate banner art", status=TaskStatus.IN_PROGRESS)

    # Initially no artifact => verifier fails
    ok, msg = verify_goal_done(spec, child, broker, {"repo_root": tmp_path})
    assert not ok
    assert "no media artifact registered" in msg

    # Create asset in media store for child.id
    mstore = MediaStore(tmp_path)
    mstore.create_asset(task_id=child.id, name="banner", content=b"banner content")

    # Now verifier passes
    ok, msg = verify_goal_done(spec, child, broker, {"repo_root": tmp_path})
    assert ok
    assert "media artifact" in msg
