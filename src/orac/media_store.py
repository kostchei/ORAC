from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from orac.models import now_iso

MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_jobs (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    workflow    TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    result_json TEXT NOT NULL DEFAULT '{}',
    error       TEXT
);

CREATE TABLE IF NOT EXISTS media_assets (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    job_id        TEXT,
    task_id       TEXT NOT NULL,
    name          TEXT NOT NULL,
    path          TEXT NOT NULL,
    mime_type     TEXT NOT NULL,
    review_state  TEXT NOT NULL DEFAULT 'generated',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    digest        TEXT
);
"""


@dataclass
class MediaJob:
    id: str
    task_id: str
    workflow: str
    prompt: str
    params: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"  # queued, running, completed, failed, cancelled
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MediaAsset:
    id: str
    task_id: str
    name: str
    path: str
    mime_type: str
    job_id: str | None = None
    review_state: str = "generated"  # generated, reviewed, archived, published
    metadata: dict[str, Any] = field(default_factory=dict)
    digest: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MediaStore:
    """Persistent storage for media jobs and generated asset artifacts."""

    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root).resolve()
        self.db_dir = self.root / ".orac"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "media.db"
        self.media_dir = self.db_dir / "outputs" / "media"
        self.archive_dir = self.media_dir / "archive"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> "MediaStore":
        with self._connect() as conn:
            conn.executescript(MEDIA_SCHEMA)
        return self

    # --- Job Queue ---

    def create_job(
        self,
        task_id: str,
        workflow: str,
        prompt: str,
        params: dict[str, Any] | None = None,
        job_id: str | None = None,
    ) -> MediaJob:
        jid = job_id or f"media-job-{uuid4().hex[:8]}"
        created = now_iso()
        params_dict = params or {}
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO media_jobs (id, created_at, updated_at, task_id, workflow, prompt, params_json, status, result_json, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', '{}', NULL)
                """,
                (jid, created, created, task_id, workflow, prompt, json.dumps(params_dict)),
            )
        return MediaJob(
            id=jid,
            task_id=task_id,
            workflow=workflow,
            prompt=prompt,
            params=params_dict,
            status="queued",
            created_at=created,
            updated_at=created,
        )

    def get_job(self, job_id: str) -> MediaJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM media_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return None
            return MediaJob(
                id=str(row["id"]),
                task_id=str(row["task_id"]),
                workflow=str(row["workflow"]),
                prompt=str(row["prompt"]),
                params=json.loads(row["params_json"]),
                status=str(row["status"]),
                result=json.loads(row["result_json"]),
                error=row["error"],
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )

    def update_job_status(
        self,
        job_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> MediaJob | None:
        updated = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE media_jobs
                SET status = ?, result_json = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, json.dumps(result or {}), error, updated, job_id),
            )
        return self.get_job(job_id)

    def list_jobs(self, task_id: str | None = None, status: str | None = None) -> list[MediaJob]:
        query = "SELECT * FROM media_jobs WHERE 1=1"
        params: list[Any] = []
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                MediaJob(
                    id=str(row["id"]),
                    task_id=str(row["task_id"]),
                    workflow=str(row["workflow"]),
                    prompt=str(row["prompt"]),
                    params=json.loads(row["params_json"]),
                    status=str(row["status"]),
                    result=json.loads(row["result_json"]),
                    error=row["error"],
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
                for row in rows
            ]

    # --- Asset Store ---

    def create_asset(
        self,
        task_id: str,
        name: str,
        content: bytes | str,
        mime_type: str = "image/png",
        job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MediaAsset:
        asset_id = f"asset-{uuid4().hex[:8]}"
        created = now_iso()
        ext = ".png" if "png" in mime_type else ".jpg" if "jpeg" in mime_type or "jpg" in mime_type else ".bin"
        file_name = f"{asset_id}_{name}{ext}"
        target_path = self.media_dir / file_name

        data_bytes = content.encode("utf-8") if isinstance(content, str) else content
        target_path.write_bytes(data_bytes)
        digest = hashlib.sha256(data_bytes).hexdigest()

        meta_dict = metadata or {}
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO media_assets (id, created_at, updated_at, job_id, task_id, name, path, mime_type, review_state, metadata_json, digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'generated', ?, ?)
                """,
                (
                    asset_id,
                    created,
                    created,
                    job_id,
                    task_id,
                    name,
                    str(target_path.resolve()),
                    mime_type,
                    json.dumps(meta_dict),
                    digest,
                ),
            )
        return MediaAsset(
            id=asset_id,
            task_id=task_id,
            name=name,
            path=str(target_path.resolve()),
            mime_type=mime_type,
            job_id=job_id,
            review_state="generated",
            metadata=meta_dict,
            digest=digest,
            created_at=created,
            updated_at=created,
        )

    def get_asset(self, asset_id: str) -> MediaAsset | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM media_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if not row:
                return None
            return MediaAsset(
                id=str(row["id"]),
                task_id=str(row["task_id"]),
                name=str(row["name"]),
                path=str(row["path"]),
                mime_type=str(row["mime_type"]),
                job_id=row["job_id"],
                review_state=str(row["review_state"]),
                metadata=json.loads(row["metadata_json"]),
                digest=row["digest"],
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )

    def update_asset_state(
        self, asset_id: str, review_state: str, notes: str | None = None
    ) -> MediaAsset | None:
        asset = self.get_asset(asset_id)
        if not asset:
            return None
        updated = now_iso()
        meta = dict(asset.metadata)
        if notes:
            meta["review_notes"] = notes
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE media_assets
                SET review_state = ?, metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (review_state, json.dumps(meta), updated, asset_id),
            )
        return self.get_asset(asset_id)

    def archive_asset(self, asset_id: str) -> MediaAsset | None:
        asset = self.get_asset(asset_id)
        if not asset:
            return None
        current_path = Path(asset.path)
        if current_path.exists():
            archive_path = self.archive_dir / current_path.name
            try:
                current_path.replace(archive_path)
                new_path_str = str(archive_path.resolve())
            except Exception:
                new_path_str = str(current_path.resolve())
        else:
            new_path_str = asset.path

        updated = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE media_assets
                SET review_state = 'archived', path = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_path_str, updated, asset_id),
            )
        return self.get_asset(asset_id)

    def list_assets(
        self, task_id: str | None = None, review_state: str | None = None
    ) -> list[MediaAsset]:
        query = "SELECT * FROM media_assets WHERE 1=1"
        params: list[Any] = []
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        if review_state is not None:
            query += " AND review_state = ?"
            params.append(review_state)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                MediaAsset(
                    id=str(row["id"]),
                    task_id=str(row["task_id"]),
                    name=str(row["name"]),
                    path=str(row["path"]),
                    mime_type=str(row["mime_type"]),
                    job_id=row["job_id"],
                    review_state=str(row["review_state"]),
                    metadata=json.loads(row["metadata_json"]),
                    digest=row["digest"],
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
                for row in rows
            ]
