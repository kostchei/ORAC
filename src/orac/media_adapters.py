from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Protocol

from orac.adapters import Adapter
from orac.media_store import MediaAsset, MediaJob, MediaStore
from orac.models import CapabilityRequest
from orac.tooling import ToolResult

MEDIA_TOOLS = frozenset(
    {
        "comfy.workflow_list",
        "comfy.generate_image",
        "comfy.queue_status",
        "comfy.fetch_artifact",
        "media.review_asset",
        "media.publish_asset",
        "media.archive_asset",
    }
)

DEFAULT_WORKFLOWS = [
    {
        "name": "txt2img_sdxl",
        "description": "Standard text-to-image pipeline using SDXL model.",
        "inputs": ["prompt", "negative_prompt", "width", "height", "steps", "seed"],
    },
    {
        "name": "img2img_sdxl",
        "description": "Image-to-image transformation pipeline using SDXL model.",
        "inputs": ["image", "prompt", "strength", "steps", "seed"],
    },
    {
        "name": "flux_dev",
        "description": "High-fidelity text-to-image generation using FLUX.1 [dev].",
        "inputs": ["prompt", "aspect_ratio", "guidance_scale", "steps"],
    },
    {
        "name": "upscale_esrgan",
        "description": "4x Real-ESRGAN super-resolution upscaler.",
        "inputs": ["image", "scale"],
    },
]


class ComfyBackend(Protocol):
    def list_workflows(self) -> list[dict[str, Any]]: ...

    def queue_prompt(
        self, workflow: str, prompt: str, params: dict[str, Any]
    ) -> dict[str, Any]: ...

    def check_status(self, job_id: str) -> dict[str, Any]: ...

    def fetch_image(self, job_id: str) -> bytes: ...


class LocalMockComfyBackend:
    """Mock/local ComfyUI execution backend for test environments and offline operation."""

    def list_workflows(self) -> list[dict[str, Any]]:
        return list(DEFAULT_WORKFLOWS)

    def queue_prompt(
        self, workflow: str, prompt: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        return {"status": "completed", "prompt": prompt, "workflow": workflow}

    def check_status(self, job_id: str) -> dict[str, Any]:
        return {"job_id": job_id, "status": "completed", "progress": 1.0}

    def fetch_image(self, job_id: str) -> bytes:
        # Generate a minimal 1x1 PNG dummy image
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff"
            b"?\x00\x05\xfe\x02\xfe\r\xefE\xb5\x00\x00\x00\x00IEND\xaeB`\x82"
        )


class MediaAdapterSet:
    """Media and ComfyUI adapters bound to a repository root."""

    def __init__(
        self,
        repo_root: Path | str,
        backend: ComfyBackend | None = None,
        store: MediaStore | None = None,
    ) -> None:
        self.root = Path(repo_root)
        self.store = store or MediaStore(self.root)
        self.backend = backend or LocalMockComfyBackend()

    def adapters(self) -> dict[str, Adapter]:
        return {
            "comfy.workflow_list": self.comfy_workflow_list,
            "comfy.generate_image": self.comfy_generate_image,
            "comfy.queue_status": self.comfy_queue_status,
            "comfy.fetch_artifact": self.comfy_fetch_artifact,
            "media.review_asset": self.media_review_asset,
            "media.publish_asset": self.media_publish_asset,
            "media.archive_asset": self.media_archive_asset,
        }

    def comfy_workflow_list(self, req: CapabilityRequest) -> ToolResult:
        workflows = self.backend.list_workflows()
        return ToolResult(
            "comfy.workflow_list",
            f"Found {len(workflows)} available ComfyUI workflow(s).",
            {"workflows": workflows},
        )

    def comfy_generate_image(self, req: CapabilityRequest) -> ToolResult:
        workflow = str(req.args.get("workflow") or "txt2img_sdxl")
        prompt = str(req.args.get("prompt") or "")
        if not prompt:
            raise ValueError("comfy.generate_image requires a non-empty 'prompt'.")
        params = dict(req.args.get("params") or {})
        for k, v in req.args.items():
            if k not in ("workflow", "prompt", "params", "task_id"):
                params[k] = v

        job = self.store.create_job(
            task_id=req.task_id,
            workflow=workflow,
            prompt=prompt,
            params=params,
        )

        try:
            backend_res = self.backend.queue_prompt(workflow, prompt, params)
            status = backend_res.get("status", "completed")
            self.store.update_job_status(job.id, status=status, result=backend_res)
        except Exception as exc:
            self.store.update_job_status(job.id, status="failed", error=str(exc))
            raise RuntimeError(f"ComfyUI submission failed: {exc}") from exc

        return ToolResult(
            "comfy.generate_image",
            f"Queued media job [{job.id}] for workflow {workflow!r}.",
            {"job_id": job.id, "status": status, "workflow": workflow},
        )

    def comfy_queue_status(self, req: CapabilityRequest) -> ToolResult:
        job_id = str(req.args.get("job_id") or "")
        if not job_id:
            raise ValueError("comfy.queue_status requires 'job_id'.")
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError(f"Media job {job_id!r} not found.")

        try:
            status_info = self.backend.check_status(job_id)
            if status_info.get("status") and status_info["status"] != job.status:
                job = self.store.update_job_status(job_id, status=status_info["status"]) or job
        except Exception:
            pass

        return ToolResult(
            "comfy.queue_status",
            f"Job [{job.id}] is currently {job.status}.",
            job.to_dict(),
        )

    def comfy_fetch_artifact(self, req: CapabilityRequest) -> ToolResult:
        job_id = str(req.args.get("job_id") or "")
        if not job_id:
            raise ValueError("comfy.fetch_artifact requires 'job_id'.")
        job = self.store.get_job(job_id)
        if not job:
            raise ValueError(f"Media job {job_id!r} not found.")
        if job.status not in ("completed", "queued", "running"):
            raise ValueError(f"Cannot fetch artifact for job in status {job.status!r}.")

        image_bytes = self.backend.fetch_image(job_id)
        asset_name = str(req.args.get("name") or f"{job.workflow}_output")
        asset = self.store.create_asset(
            task_id=req.task_id,
            name=asset_name,
            content=image_bytes,
            mime_type="image/png",
            job_id=job.id,
            metadata={"workflow": job.workflow, "prompt": job.prompt},
        )

        rollback_contract = {
            "version": 1,
            "tool": "media.archive_asset",
            "args": {"asset_id": asset.id},
            "expected_state": {"review_state": "generated"},
            "expires_at": None,
            "operator_prompt": f"Archive generated media asset {asset.id}?",
        }

        return ToolResult(
            "comfy.fetch_artifact",
            f"Fetched and stored media asset [{asset.id}] at {asset.path}.",
            {
                "asset_id": asset.id,
                "path": asset.path,
                "review_state": asset.review_state,
                "digest": asset.digest,
                "rollback_contract": rollback_contract,
            },
        )

    def media_review_asset(self, req: CapabilityRequest) -> ToolResult:
        asset_id = str(req.args.get("asset_id") or "")
        if not asset_id:
            raise ValueError("media.review_asset requires 'asset_id'.")
        notes = req.args.get("notes")
        asset = self.store.update_asset_state(asset_id, review_state="reviewed", notes=notes)
        if not asset:
            raise ValueError(f"Asset {asset_id!r} not found in media store.")
        return ToolResult(
            "media.review_asset",
            f"Asset [{asset.id}] moved to review_state 'reviewed'.",
            asset.to_dict(),
        )

    def media_publish_asset(self, req: CapabilityRequest) -> ToolResult:
        asset_id = str(req.args.get("asset_id") or "")
        if not asset_id:
            raise ValueError("media.publish_asset requires 'asset_id'.")
        asset = self.store.get_asset(asset_id)
        if not asset:
            raise ValueError(f"Asset {asset_id!r} not found in media store.")

        destination = str(req.args.get("destination") or "outputs/published")
        dest_dir = self.root / destination
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / Path(asset.path).name
        shutil.copy2(asset.path, dest_file)

        updated_asset = self.store.update_asset_state(
            asset_id,
            review_state="published",
            notes=f"Published to {dest_file.resolve()}",
        )

        return ToolResult(
            "media.publish_asset",
            f"Asset [{asset.id}] published to {dest_file.resolve()}.",
            {
                "asset_id": asset.id,
                "published_path": str(dest_file.resolve()),
                "review_state": "published",
            },
        )

    def media_archive_asset(self, req: CapabilityRequest) -> ToolResult:
        asset_id = str(req.args.get("asset_id") or "")
        if not asset_id:
            raise ValueError("media.archive_asset requires 'asset_id'.")
        asset = self.store.archive_asset(asset_id)
        if not asset:
            raise ValueError(f"Asset {asset_id!r} not found in media store.")
        return ToolResult(
            "media.archive_asset",
            f"Asset [{asset.id}] archived at {asset.path}.",
            asset.to_dict(),
        )


def media_adapters_for(
    repo_root: Path | str,
    backend: ComfyBackend | None = None,
    store: MediaStore | None = None,
) -> dict[str, Adapter]:
    return MediaAdapterSet(repo_root, backend=backend, store=store).adapters()
