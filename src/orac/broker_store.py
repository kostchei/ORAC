from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orac.agent_registry import load_agent_profiles
from orac.models import CapabilityRequest, CapabilityResult, CouncilVerdict, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    agent       TEXT NOT NULL,
    tool        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manifest',
    granted_at  TEXT NOT NULL,
    PRIMARY KEY (agent, tool)
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    agent       TEXT NOT NULL,
    tool        TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    status      TEXT NOT NULL,
    message     TEXT NOT NULL,
    args_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    agent       TEXT NOT NULL,
    tool        TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    agent       TEXT NOT NULL,
    tool        TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    lens        TEXT NOT NULL,
    decision    TEXT NOT NULL,
    reason      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    agent       TEXT NOT NULL,
    tool        TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    message     TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    acked       INTEGER NOT NULL DEFAULT 0,
    acked_at    TEXT
);

CREATE TABLE IF NOT EXISTS rate_counters (
    agent       TEXT NOT NULL,
    tool        TEXT NOT NULL,
    day         TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent, tool, day)
);
"""


@dataclass(frozen=True)
class AuditEntry:
    id: int
    created_at: str
    agent: str
    tool: str
    task_id: str
    status: str
    message: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Notification:
    id: int
    created_at: str
    agent: str
    tool: str
    task_id: str
    message: str
    args: dict[str, Any]
    acked: bool
    acked_at: str | None


@dataclass(frozen=True)
class PendingApproval:
    id: int
    created_at: str
    agent: str
    tool: str
    task_id: str
    args: dict[str, Any]
    status: str
    resolved_at: str | None


class BrokerStore:
    """SQLite-backed state for the broker.

    Grants, audit, pending-approvals, and per-day rate counters are written
    concurrently by the daemon and the UI, so they live in a single WAL-mode
    SQLite database rather than the flat JSON board. This is the durable
    foundation the ``pending`` approval path and real adapters build on.
    """

    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root)
        self.state_dir = self.root / ".orac"
        self.db_path = self.state_dir / "broker.db"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def init(self) -> "BrokerStore":
        """Create the schema and seed manifest grants on first use."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._seed_manifest_grants(conn)
        return self

    def _seed_manifest_grants(self, conn: sqlite3.Connection) -> None:
        existing = conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0]
        if existing:
            return
        ts = now_iso()
        rows = [
            (profile.name, tool, "manifest", ts)
            for profile in load_agent_profiles()
            for tool in profile.tools
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO grants (agent, tool, source, granted_at) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )

    # --- grants -----------------------------------------------------------

    def grants(self) -> dict[str, frozenset[str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT agent, tool FROM grants").fetchall()
        mapping: dict[str, set[str]] = {}
        for row in rows:
            mapping.setdefault(row["agent"], set()).add(row["tool"])
        return {agent: frozenset(tools) for agent, tools in mapping.items()}

    def grant(self, agent: str, tool: str, source: str = "user") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO grants (agent, tool, source, granted_at) "
                "VALUES (?, ?, ?, ?)",
                (agent, tool, source, now_iso()),
            )

    def revoke(self, agent: str, tool: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM grants WHERE agent = ? AND tool = ?", (agent, tool)
            )

    # --- audit ------------------------------------------------------------

    def record_audit(self, req: CapabilityRequest, result: CapabilityResult) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit "
                "(created_at, agent, tool, task_id, status, message, args_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now_iso(),
                    req.agent,
                    req.tool,
                    req.task_id,
                    result.status.value,
                    result.message,
                    json.dumps(req.args, sort_keys=True),
                ),
            )

    def audit_log(self, limit: int = 100) -> list[AuditEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            AuditEntry(
                id=row["id"],
                created_at=row["created_at"],
                agent=row["agent"],
                tool=row["tool"],
                task_id=row["task_id"],
                status=row["status"],
                message=row["message"],
                args=json.loads(row["args_json"]),
            )
            for row in rows
        ]

    # --- audit queries used by the deterministic council lenses ------------

    def audit_count(self, agent: str, tool: str, task_id: str) -> int:
        """How many times this agent has successfully used this tool on this task."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM audit "
                "WHERE agent = ? AND tool = ? AND task_id = ? AND status = 'allowed'",
                (agent, tool, task_id),
            ).fetchone()
        return int(row[0])

    def audit_count_exact(
        self, agent: str, tool: str, task_id: str, args_json: str
    ) -> int:
        """How many times this exact call (same args) already succeeded."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM audit "
                "WHERE agent = ? AND tool = ? AND task_id = ? AND args_json = ? "
                "AND status = 'allowed'",
                (agent, tool, task_id, args_json),
            ).fetchone()
        return int(row[0])

    # --- council reviews ----------------------------------------------------

    def record_review(self, req: CapabilityRequest, verdict: CouncilVerdict) -> None:
        """Persist the per-lens verdicts for a non-clean council review.

        One row per lens, so every block or escalation is explainable later.
        """
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO reviews "
                "(created_at, agent, tool, task_id, lens, decision, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        now_iso(),
                        req.agent,
                        req.tool,
                        req.task_id,
                        lens.lens,
                        lens.decision.value,
                        lens.reason,
                    )
                    for lens in verdict.lenses
                ],
            )

    def list_reviews(self, task_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM reviews"
        params: tuple[Any, ...] = ()
        if task_id is not None:
            query += " WHERE task_id = ?"
            params = (task_id,)
        query += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # --- notifications (the review-after queue) ----------------------------

    def record_notification(
        self, req: CapabilityRequest, result: CapabilityResult
    ) -> int:
        """Queue a completed action for retrospective human review.

        This is the "I did X, here is the result — ok?" surface: the action has
        already run; the human reviews after the fact and can roll back.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO notifications "
                "(created_at, agent, tool, task_id, message, args_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    now_iso(),
                    req.agent,
                    req.tool,
                    req.task_id,
                    result.message,
                    json.dumps(req.args, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def list_notifications(self, unacked_only: bool = True) -> list[Notification]:
        query = "SELECT * FROM notifications"
        if unacked_only:
            query += " WHERE acked = 0"
        query += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [
            Notification(
                id=row["id"],
                created_at=row["created_at"],
                agent=row["agent"],
                tool=row["tool"],
                task_id=row["task_id"],
                message=row["message"],
                args=json.loads(row["args_json"]),
                acked=bool(row["acked"]),
                acked_at=row["acked_at"],
            )
            for row in rows
        ]

    def ack_notification(self, notification_id: int) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE notifications SET acked = 1, acked_at = ? "
                "WHERE id = ? AND acked = 0",
                (now_iso(), notification_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"No unacked notification {notification_id}.")

    # --- pending approvals ------------------------------------------------

    def create_pending(self, req: CapabilityRequest) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO pending_approvals "
                "(created_at, agent, tool, task_id, args_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    now_iso(),
                    req.agent,
                    req.tool,
                    req.task_id,
                    json.dumps(req.args, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def get_pending(self, pending_id: int) -> PendingApproval:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE id = ?", (pending_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"No pending approval {pending_id}.")
        return self._pending_from_row(row)

    def approval_status(self, req: CapabilityRequest) -> str | None:
        """Return the approval state for an exact (agent, tool, args) request.

        ``approved`` if a matching request was approved, ``pending`` if one is
        still queued, otherwise ``None``. Matching on the canonical args lets the
        same call be re-issued after approval and resolve to allowed.
        """
        args_json = json.dumps(req.args, sort_keys=True)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status FROM pending_approvals "
                "WHERE agent = ? AND tool = ? AND args_json = ?",
                (req.agent, req.tool, args_json),
            ).fetchall()
        statuses = {row["status"] for row in rows}
        if "approved" in statuses:
            return "approved"
        if "pending" in statuses:
            return "pending"
        return None

    def get_pending_id(self, req: CapabilityRequest) -> int:
        """Return the id of the open pending row for this exact request."""
        args_json = json.dumps(req.args, sort_keys=True)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM pending_approvals "
                "WHERE agent = ? AND tool = ? AND args_json = ? AND status = 'pending' "
                "ORDER BY id DESC LIMIT 1",
                (req.agent, req.tool, args_json),
            ).fetchone()
        if row is None:
            raise KeyError("No open pending approval for request.")
        return int(row["id"])

    def list_pending(self) -> list[PendingApproval]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_approvals WHERE status = 'pending' "
                "ORDER BY id ASC"
            ).fetchall()
        return [self._pending_from_row(row) for row in rows]

    def resolve_pending(self, pending_id: int, status: str) -> None:
        if status not in {"approved", "denied", "expired"}:
            raise ValueError(f"Invalid pending resolution {status!r}.")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE pending_approvals SET status = ?, resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, now_iso(), pending_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"No pending approval {pending_id} to resolve.")

    def _pending_from_row(self, row: sqlite3.Row) -> PendingApproval:
        return PendingApproval(
            id=row["id"],
            created_at=row["created_at"],
            agent=row["agent"],
            tool=row["tool"],
            task_id=row["task_id"],
            args=json.loads(row["args_json"]),
            status=row["status"],
            resolved_at=row["resolved_at"],
        )

    # --- rate counters ----------------------------------------------------

    def bump_rate(self, agent: str, tool: str, day: str) -> int:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rate_counters (agent, tool, day, count) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(agent, tool, day) DO UPDATE SET count = count + 1",
                (agent, tool, day),
            )
            row = conn.execute(
                "SELECT count FROM rate_counters "
                "WHERE agent = ? AND tool = ? AND day = ?",
                (agent, tool, day),
            ).fetchone()
        return int(row["count"])

    def rate_count(self, agent: str, tool: str, day: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count FROM rate_counters "
                "WHERE agent = ? AND tool = ? AND day = ?",
                (agent, tool, day),
            ).fetchone()
        return int(row["count"]) if row else 0
