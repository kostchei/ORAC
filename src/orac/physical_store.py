from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from orac.models import now_iso

PHYSICAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS physical_devices (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    entity_id        TEXT NOT NULL,
    backend          TEXT NOT NULL DEFAULT 'mock',
    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    last_action_at   TEXT,
    metadata_json    TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prepared_actions (
    id                TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL,
    device_id         TEXT NOT NULL,
    domain            TEXT NOT NULL DEFAULT 'homeassistant',
    service           TEXT NOT NULL,
    target_state_json TEXT NOT NULL DEFAULT '{}',
    pre_state_json    TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'prepared',
    expires_at        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS physical_actions (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    action_type  TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
"""

DEFAULT_DEVICES = [
    {
        "id": "feeder_1",
        "name": "Aquarium Fish Feeder",
        "entity_id": "switch.fish_feeder",
        "backend": "mock",
        "cooldown_seconds": 300,
        "metadata": {"location": "aquarium", "supports_inverse": False},
    },
    {
        "id": "living_room_light",
        "name": "Living Room Main Light",
        "entity_id": "light.living_room",
        "backend": "mock",
        "cooldown_seconds": 5,
        "metadata": {"location": "living_room", "supports_inverse": True},
    },
    {
        "id": "thermostat",
        "name": "Main Thermostat",
        "entity_id": "climate.thermostat",
        "backend": "mock",
        "cooldown_seconds": 60,
        "metadata": {"location": "hallway", "supports_inverse": True},
    },
]


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


@dataclass
class PhysicalDevice:
    id: str
    name: str
    entity_id: str
    backend: str = "mock"
    cooldown_seconds: int = 60
    last_action_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreparedAction:
    id: str
    task_id: str
    device_id: str
    service: str
    domain: str = "homeassistant"
    target_state: dict[str, Any] = field(default_factory=dict)
    pre_state: dict[str, Any] = field(default_factory=dict)
    status: str = "prepared"  # prepared, executed, expired, cancelled
    expires_at: str = field(default_factory=now_iso)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def is_expired(self, now: datetime | None = None) -> bool:
        current_time = now or datetime.now(timezone.utc)
        exp = _parse_iso(self.expires_at)
        if exp is None:
            return False
        return current_time > exp

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PhysicalActionRecord:
    id: str
    task_id: str
    device_id: str
    action_type: str
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PhysicalStore:
    """Persistent storage for physical device allowlist, cooldowns, and prepared actions."""

    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root).resolve()
        self.db_dir = self.root / ".orac"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "physical.db"
        self.config_path = self.db_dir / "physical_devices.json"
        self.init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> "PhysicalStore":
        with self._connect() as conn:
            conn.executescript(PHYSICAL_SCHEMA)
        self._load_or_seed_devices()
        return self

    def _load_or_seed_devices(self) -> None:
        # If external config exists, sync from it
        if self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                devices = data if isinstance(data, list) else data.get("devices", [])
                for d in devices:
                    self.register_device(
                        device_id=d["id"],
                        name=d.get("name", d["id"]),
                        entity_id=d.get("entity_id", d["id"]),
                        backend=d.get("backend", "mock"),
                        cooldown_seconds=int(d.get("cooldown_seconds", 60)),
                        metadata=d.get("metadata", {}),
                    )
                return
            except Exception:
                pass

        # If DB is empty, seed defaults
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM physical_devices").fetchone()[0]
            if count == 0:
                for d in DEFAULT_DEVICES:
                    self.register_device(
                        device_id=d["id"],
                        name=d["name"],
                        entity_id=d["entity_id"],
                        backend=d["backend"],
                        cooldown_seconds=d["cooldown_seconds"],
                        metadata=d["metadata"],
                    )

    # --- Device Allowlist & Registry ---

    def register_device(
        self,
        device_id: str,
        name: str,
        entity_id: str,
        backend: str = "mock",
        cooldown_seconds: int = 60,
        metadata: dict[str, Any] | None = None,
    ) -> PhysicalDevice:
        now = now_iso()
        meta = metadata or {}
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO physical_devices (id, name, entity_id, backend, cooldown_seconds, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    entity_id = excluded.entity_id,
                    backend = excluded.backend,
                    cooldown_seconds = excluded.cooldown_seconds,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    device_id,
                    name,
                    entity_id,
                    backend,
                    cooldown_seconds,
                    json.dumps(meta),
                    now,
                    now,
                ),
            )
        return self.get_device(device_id)  # type: ignore[return-value]

    def unregister_device(self, device_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM physical_devices WHERE id = ?", (device_id,))
            return cur.rowcount > 0

    def get_device(self, device_id: str) -> PhysicalDevice | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM physical_devices WHERE id = ?", (device_id,)
            ).fetchone()
            if not row:
                return None
            return PhysicalDevice(
                id=str(row["id"]),
                name=str(row["name"]),
                entity_id=str(row["entity_id"]),
                backend=str(row["backend"]),
                cooldown_seconds=int(row["cooldown_seconds"]),
                last_action_at=row["last_action_at"],
                metadata=json.loads(row["metadata_json"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )

    def list_devices(self) -> list[PhysicalDevice]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM physical_devices ORDER BY created_at ASC"
            ).fetchall()
            return [
                PhysicalDevice(
                    id=str(row["id"]),
                    name=str(row["name"]),
                    entity_id=str(row["entity_id"]),
                    backend=str(row["backend"]),
                    cooldown_seconds=int(row["cooldown_seconds"]),
                    last_action_at=row["last_action_at"],
                    metadata=json.loads(row["metadata_json"]),
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
                for row in rows
            ]

    # --- Cooldown Management ---

    def is_in_cooldown(
        self, device_id: str, now: datetime | None = None
    ) -> tuple[bool, float]:
        """Check if device is currently within its cooldown period.

        Returns (in_cooldown, remaining_seconds).
        """
        device = self.get_device(device_id)
        if not device or not device.last_action_at:
            return False, 0.0

        last_action = _parse_iso(device.last_action_at)
        if not last_action:
            return False, 0.0

        current_time = now or datetime.now(timezone.utc)
        elapsed = (current_time - last_action).total_seconds()
        remaining = float(device.cooldown_seconds) - elapsed
        if remaining > 0:
            return True, remaining
        return False, 0.0

    def record_device_action(
        self, device_id: str, action_time: str | None = None
    ) -> None:
        ts = action_time or now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE physical_devices
                SET last_action_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (ts, ts, device_id),
            )

    # --- Prepared Actions ---

    def create_prepared_action(
        self,
        task_id: str,
        device_id: str,
        service: str,
        domain: str = "homeassistant",
        target_state: dict[str, Any] | None = None,
        pre_state: dict[str, Any] | None = None,
        ttl_seconds: int = 900,
        action_id: str | None = None,
    ) -> PreparedAction:
        aid = action_id or f"prep-{uuid4().hex[:8]}"
        created = now_iso()
        now_dt = datetime.now(timezone.utc)
        expires_dt = datetime.fromtimestamp(now_dt.timestamp() + ttl_seconds, tz=timezone.utc)
        expires_at = expires_dt.isoformat()
        tgt = target_state or {}
        pre = pre_state or {}

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO prepared_actions (
                    id, task_id, device_id, domain, service,
                    target_state_json, pre_state_json, status, expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?)
                """,
                (
                    aid,
                    task_id,
                    device_id,
                    domain,
                    service,
                    json.dumps(tgt),
                    json.dumps(pre),
                    expires_at,
                    created,
                    created,
                ),
            )
        return PreparedAction(
            id=aid,
            task_id=task_id,
            device_id=device_id,
            domain=domain,
            service=service,
            target_state=tgt,
            pre_state=pre,
            status="prepared",
            expires_at=expires_at,
            created_at=created,
            updated_at=created,
        )

    def get_prepared_action(self, action_id: str) -> PreparedAction | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM prepared_actions WHERE id = ?", (action_id,)
            ).fetchone()
            if not row:
                return None
            return PreparedAction(
                id=str(row["id"]),
                task_id=str(row["task_id"]),
                device_id=str(row["device_id"]),
                domain=str(row["domain"]),
                service=str(row["service"]),
                target_state=json.loads(row["target_state_json"]),
                pre_state=json.loads(row["pre_state_json"]),
                status=str(row["status"]),
                expires_at=str(row["expires_at"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )

    def update_prepared_action_status(
        self, action_id: str, status: str
    ) -> PreparedAction | None:
        updated = now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE prepared_actions
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, updated, action_id),
            )
        return self.get_prepared_action(action_id)

    # --- Audit Logging ---

    def record_action(
        self,
        task_id: str,
        device_id: str,
        action_type: str,
        details: dict[str, Any] | None = None,
    ) -> PhysicalActionRecord:
        pid = f"pact-{uuid4().hex[:8]}"
        created = now_iso()
        det = details or {}
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO physical_actions (id, task_id, device_id, action_type, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pid, task_id, device_id, action_type, json.dumps(det), created),
            )
        return PhysicalActionRecord(
            id=pid,
            task_id=task_id,
            device_id=device_id,
            action_type=action_type,
            details=det,
            created_at=created,
        )

    def list_actions(
        self, task_id: str | None = None, device_id: str | None = None
    ) -> list[PhysicalActionRecord]:
        query = "SELECT * FROM physical_actions WHERE 1=1"
        params: list[Any] = []
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        if device_id is not None:
            query += " AND device_id = ?"
            params.append(device_id)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                PhysicalActionRecord(
                    id=str(row["id"]),
                    task_id=str(row["task_id"]),
                    device_id=str(row["device_id"]),
                    action_type=str(row["action_type"]),
                    details=json.loads(row["details_json"]),
                    created_at=str(row["created_at"]),
                )
                for row in rows
            ]
