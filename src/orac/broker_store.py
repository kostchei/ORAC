from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    data_json   TEXT NOT NULL DEFAULT '{}',
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

CREATE TABLE IF NOT EXISTS standing_grants (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    agent        TEXT NOT NULL,
    tool         TEXT NOT NULL,
    args_pattern TEXT,
    daily_cap    INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0,
    revoked_at   TEXT
);

CREATE TABLE IF NOT EXISTS subagents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    parent_task_id TEXT NOT NULL,
    profile_slug   TEXT NOT NULL,
    instruction    TEXT NOT NULL,
    intent         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    resolved_at    TEXT
);

CREATE TABLE IF NOT EXISTS tunables (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# The roster cap. This is both the deterministic admission limit AND the number
# the Orchestrator's abundance frame is built on (design: "you have N/MAX slots
# free" biases toward decomposition). The two must stay equal so the frame is
# honest — never tell the model a budget the register will not honour.
MAX_SUBAGENTS = 500

# A spawned subagent that is still drawing (or holding) resources.
_LIVE_SUBAGENT_STATUSES = ("proposed", "active")
STALE_SUBAGENT_SECONDS = 10 * 60


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
    data: dict[str, Any]
    acked: bool
    acked_at: str | None


@dataclass(frozen=True)
class Subagent:
    """One spawned subagent instance in the register (design: the ≤500 roster).

    Distinct from a static agent *profile* (`agent_registry`): a profile is a
    role definition; a Subagent is a live instance doing one decomposed slice of
    work, with its own instruction and intent slice.
    """

    id: int
    created_at: str
    parent_task_id: str
    profile_slug: str
    instruction: str
    intent: str
    status: str
    resolved_at: str | None


@dataclass(frozen=True)
class StandingGrant:
    """A pre-authorisation that lets a recurring action run without parking for a
    human each time, capped per day (the fish-feeder case, design P6).

    ``args_pattern`` None matches any arguments; otherwise it is the canonical
    args JSON the call must match exactly. A standing grant short-circuits the
    risk model's APPROVE gate only — it never bypasses the council's safety
    floor (Sentinel/Optimise/Simple/Efficiency), so it cannot authorise the
    system to edit its own governor or exceed the fair-share band.
    """

    id: int
    created_at: str
    agent: str
    tool: str
    args_pattern: str | None
    daily_cap: int
    reason: str
    revoked: bool
    revoked_at: str | None


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
            self._migrate(conn)
            self._seed_manifest_grants(conn)
        return self

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Upgrade pre-existing databases created before a schema column existed."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(notifications)")}
        if "data_json" not in columns:
            conn.execute(
                "ALTER TABLE notifications "
                "ADD COLUMN data_json TEXT NOT NULL DEFAULT '{}'"
            )

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

    def list_reviews(
        self, task_id: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM reviews"
        params: tuple[Any, ...] = ()
        if task_id is not None:
            query += " WHERE task_id = ?"
            params = (task_id,)
        if limit is None:
            query += " ORDER BY id ASC"
        else:
            # Recent-first when capped: the cockpit shows the latest verdicts.
            query += " ORDER BY id DESC LIMIT ?"
            params = (*params, limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # --- notifications (the review-after queue) ----------------------------

    def record_notification(
        self, req: CapabilityRequest, result: CapabilityResult
    ) -> int:
        """Queue a completed action for retrospective human review.

        This is the "I did X, here is the result — ok?" surface: the action has
        already run; the human reviews after the fact and can roll back. The
        result data is kept so the review surface can act on it (e.g. the pushed
        commit sha is what `orac rollback` reverts).
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO notifications "
                "(created_at, agent, tool, task_id, message, args_json, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now_iso(),
                    req.agent,
                    req.tool,
                    req.task_id,
                    result.message,
                    json.dumps(req.args, sort_keys=True),
                    json.dumps(result.data, sort_keys=True, default=str),
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
                data=json.loads(row["data_json"]),
                acked=bool(row["acked"]),
                acked_at=row["acked_at"],
            )
            for row in rows
        ]

    def get_notification(self, notification_id: int) -> Notification:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"No notification {notification_id}.")
        return Notification(
            id=row["id"],
            created_at=row["created_at"],
            agent=row["agent"],
            tool=row["tool"],
            task_id=row["task_id"],
            message=row["message"],
            args=json.loads(row["args_json"]),
            data=json.loads(row["data_json"]),
            acked=bool(row["acked"]),
            acked_at=row["acked_at"],
        )

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

    # --- standing grants (P6: pre-authorised recurring intent) -------------

    def create_standing_grant(
        self,
        agent: str,
        tool: str,
        daily_cap: int,
        reason: str,
        args: dict[str, Any] | None = None,
    ) -> int:
        """Pre-authorise up to ``daily_cap`` runs/day of (agent, tool[, args]).

        ``args`` None grants any arguments; a dict pins the grant to that exact
        call. ``daily_cap`` must be positive — a zero/negative cap is not a grant.
        """
        if daily_cap <= 0:
            raise ValueError("A standing grant needs a positive daily cap.")
        pattern = None if args is None else json.dumps(args, sort_keys=True)
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO standing_grants "
                "(created_at, agent, tool, args_pattern, daily_cap, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso(), agent, tool, pattern, daily_cap, reason),
            )
            return int(cursor.lastrowid)

    def list_standing_grants(self, active_only: bool = True) -> list[StandingGrant]:
        query = "SELECT * FROM standing_grants"
        if active_only:
            query += " WHERE revoked = 0"
        query += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [self._standing_from_row(row) for row in rows]

    def revoke_standing_grant(self, grant_id: int) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE standing_grants SET revoked = 1, revoked_at = ? "
                "WHERE id = ? AND revoked = 0",
                (now_iso(), grant_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"No active standing grant {grant_id} to revoke.")

    def standing_grant_for(
        self, agent: str, tool: str, args: dict[str, Any]
    ) -> StandingGrant | None:
        """The active standing grant covering this exact call, if any.

        An args-pinned grant must match the canonical args exactly; an
        unpinned (NULL pattern) grant covers any args for that (agent, tool).
        The most specific (pinned) match wins so a narrow grant is preferred.
        """
        args_json = json.dumps(args, sort_keys=True)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM standing_grants "
                "WHERE revoked = 0 AND agent = ? AND tool = ? "
                "AND (args_pattern IS NULL OR args_pattern = ?) "
                "ORDER BY args_pattern IS NULL, id ASC",
                (agent, tool, args_json),
            ).fetchall()
        return self._standing_from_row(rows[0]) if rows else None

    def _standing_from_row(self, row: sqlite3.Row) -> StandingGrant:
        return StandingGrant(
            id=row["id"],
            created_at=row["created_at"],
            agent=row["agent"],
            tool=row["tool"],
            args_pattern=row["args_pattern"],
            daily_cap=row["daily_cap"],
            reason=row["reason"],
            revoked=bool(row["revoked"]),
            revoked_at=row["revoked_at"],
        )

    # --- subagent register (the ≤500 roster) -------------------------------

    def subagent_roster_count(self) -> int:
        """How many subagents are still live (proposed or active)."""
        placeholders = ", ".join("?" for _ in _LIVE_SUBAGENT_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM subagents WHERE status IN ({placeholders})",
                _LIVE_SUBAGENT_STATUSES,
            ).fetchone()
        return int(row[0])

    def subagent_free_slots(self, cap: int = MAX_SUBAGENTS) -> int:
        """Slots left on the roster — the honest number behind the frame."""
        return max(0, cap - self.subagent_roster_count())

    def reap_stale_subagents(
        self,
        *,
        older_than_seconds: int = STALE_SUBAGENT_SECONDS,
        status: str = "blocked",
    ) -> int:
        """Retire active/proposed subagent reservations that cannot still be live."""
        if status not in {"done", "blocked", "retired"}:
            raise ValueError(f"Invalid stale subagent status {status!r}.")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        ).replace(microsecond=0).isoformat()
        resolved = now_iso()
        placeholders = ", ".join("?" for _ in _LIVE_SUBAGENT_STATUSES)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE subagents SET status = ?, resolved_at = ? "
                f"WHERE status IN ({placeholders}) AND created_at < ?",
                (status, resolved, *_LIVE_SUBAGENT_STATUSES, cutoff),
            )
            return int(cursor.rowcount or 0)

    def admit_subagent(
        self,
        parent_task_id: str,
        profile_slug: str,
        instruction: str,
        intent: str,
        cap: int = MAX_SUBAGENTS,
    ) -> int:
        """Admit a subagent to the roster, enforcing the cap. Raises if full.

        Admission control is deterministic and fail-closed: a full roster is a
        real limit, not something to silently grow past. Callers (Optimise's
        allocator) check ``subagent_free_slots`` first; this is the hard floor.
        """
        if self.subagent_roster_count() >= cap:
            raise RuntimeError(
                f"Subagent roster is full ({cap}); cannot admit another. "
                "Retire finished subagents or wait for slots to free."
            )
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO subagents "
                "(created_at, parent_task_id, profile_slug, instruction, intent, "
                "status) VALUES (?, ?, ?, ?, ?, 'active')",
                (now_iso(), parent_task_id, profile_slug, instruction, intent),
            )
            return int(cursor.lastrowid)

    def set_subagent_status(self, subagent_id: int, status: str) -> None:
        if status not in {"proposed", "active", "done", "blocked", "retired"}:
            raise ValueError(f"Invalid subagent status {status!r}.")
        resolved = now_iso() if status in {"done", "blocked", "retired"} else None
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE subagents SET status = ?, resolved_at = ? WHERE id = ?",
                (status, resolved, subagent_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"No subagent {subagent_id}.")

    def list_subagents(self, status: str | None = None) -> list[Subagent]:
        query = "SELECT * FROM subagents"
        params: tuple[Any, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._subagent_from_row(row) for row in rows]

    def _subagent_from_row(self, row: sqlite3.Row) -> Subagent:
        return Subagent(
            id=row["id"],
            created_at=row["created_at"],
            parent_task_id=row["parent_task_id"],
            profile_slug=row["profile_slug"],
            instruction=row["instruction"],
            intent=row["intent"],
            status=row["status"],
            resolved_at=row["resolved_at"],
        )

    # --- tunables (self-tuning knobs; performance only, never safety) -------

    def get_tunable(self, key: str, default: str) -> str:
        """Read a tunable's value, or ``default`` if it has never been set.

        Tunables are performance knobs (e.g. the decomposition threshold) that
        the self-tuning loop may adjust within hard bounds. They are deliberately
        separate from grants and risk classes: tuning here can never weaken the
        safety floor, only change how eagerly the system fans work out.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM tunables WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row is not None else default

    def set_tunable(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tunables (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, now_iso()),
            )
