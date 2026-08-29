from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4

from orac.models import now_iso

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_sessions (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'created',
    current_round INTEGER NOT NULL DEFAULT 0,
    total_rounds  INTEGER NOT NULL DEFAULT 1,
    state_json    TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_participants (
    id          TEXT PRIMARY KEY,
    event_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'participant',
    channel     TEXT NOT NULL DEFAULT 'local',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_rounds (
    id             TEXT PRIMARY KEY,
    event_id       TEXT NOT NULL,
    round_number   INTEGER NOT NULL,
    title          TEXT NOT NULL,
    prompt         TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',
    responses_json TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_human_requests (
    id              TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL,
    participant_id  TEXT,
    question        TEXT NOT NULL,
    options_json    TEXT NOT NULL DEFAULT '[]',
    timeout_seconds INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    response        TEXT,
    pending_id      INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_broadcasts (
    id           TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL,
    round_number INTEGER NOT NULL DEFAULT 0,
    channel      TEXT NOT NULL DEFAULT 'all',
    message      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


@dataclass
class EventSession:
    id: str
    task_id: str
    title: str
    status: str = "created"  # created, active, waiting_human, completed, cancelled
    current_round: int = 0
    total_rounds: int = 1
    state: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventParticipant:
    id: str
    event_id: str
    name: str
    role: str = "participant"  # host, participant, observer, judge
    channel: str = "local"     # local, slack, whatsapp
    status: str = "active"     # active, paused, left
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventRound:
    id: str
    event_id: str
    round_number: int
    title: str
    prompt: str = ""
    status: str = "active"  # active, completed, skipped
    responses: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HumanInputRequest:
    id: str
    event_id: str
    question: str
    options: list[str] = field(default_factory=list)
    participant_id: str | None = None
    timeout_seconds: int = 0
    status: str = "pending"  # pending, answered, timed_out, cancelled
    response: str | None = None
    pending_id: int | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BroadcastMessage:
    id: str
    event_id: str
    round_number: int
    channel: str
    message: str
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventsStore:
    """Persistent store for human events, interactive sessions, rounds, and participant workflows."""

    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root).resolve()
        self.db_dir = self.root / ".orac"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "events.db"
        self.init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> "EventsStore":
        with self._connect() as conn:
            conn.executescript(EVENTS_SCHEMA)
            conn.commit()
        return self

    def create_event(
        self,
        title: str,
        total_rounds: int = 1,
        task_id: str = "",
        metadata: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> EventSession:
        ev_id = event_id or uuid4().hex[:10]
        now = now_iso()
        state = metadata or {}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_sessions (id, task_id, title, status, current_round, total_rounds, state_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ev_id, task_id, title, "active", 1 if total_rounds > 0 else 0, total_rounds, json.dumps(state), now, now),
            )
            # Create initial round 1 if total_rounds >= 1
            if total_rounds >= 1:
                r_id = uuid4().hex[:10]
                conn.execute(
                    "INSERT INTO event_rounds (id, event_id, round_number, title, prompt, status, responses_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r_id, ev_id, 1, f"Round 1: {title}", "", "active", "{}", now, now),
                )
            conn.commit()
        return self.get_event(ev_id)

    def get_event(self, event_id: str) -> EventSession:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM event_sessions WHERE id = ?", (event_id,)).fetchone()
            if not row:
                raise KeyError(f"No event session found for id {event_id!r}")
            return EventSession(
                id=row["id"],
                task_id=row["task_id"],
                title=row["title"],
                status=row["status"],
                current_round=int(row["current_round"]),
                total_rounds=int(row["total_rounds"]),
                state=json.loads(row["state_json"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def list_events(self, task_id: str | None = None, status: str | None = None) -> list[EventSession]:
        query = "SELECT * FROM event_sessions WHERE 1=1"
        params: list[Any] = []
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                EventSession(
                    id=r["id"],
                    task_id=r["task_id"],
                    title=r["title"],
                    status=r["status"],
                    current_round=int(r["current_round"]),
                    total_rounds=int(r["total_rounds"]),
                    state=json.loads(r["state_json"]),
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
                for r in rows
            ]

    def add_participant(
        self,
        event_id: str,
        name: str,
        role: str = "participant",
        channel: str = "local",
    ) -> EventParticipant:
        # Verify event exists
        self.get_event(event_id)
        p_id = uuid4().hex[:10]
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_participants (id, event_id, name, role, channel, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (p_id, event_id, name, role, channel, "active", now),
            )
            conn.commit()
        return EventParticipant(id=p_id, event_id=event_id, name=name, role=role, channel=channel, status="active", created_at=now)

    def list_participants(self, event_id: str) -> list[EventParticipant]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM event_participants WHERE event_id = ? ORDER BY created_at ASC", (event_id,)).fetchall()
            return [
                EventParticipant(
                    id=r["id"],
                    event_id=r["event_id"],
                    name=r["name"],
                    role=r["role"],
                    channel=r["channel"],
                    status=r["status"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def advance_round(
        self,
        event_id: str,
        summary: str = "",
        title: str = "",
        prompt: str = "",
    ) -> EventSession:
        ev = self.get_event(event_id)
        now = now_iso()
        with self._connect() as conn:
            # Mark current round completed
            conn.execute(
                "UPDATE event_rounds SET status = 'completed', updated_at = ? WHERE event_id = ? AND round_number = ?",
                (now, event_id, ev.current_round),
            )
            next_round = ev.current_round + 1
            if next_round > ev.total_rounds:
                conn.execute(
                    "UPDATE event_sessions SET status = 'completed', updated_at = ? WHERE id = ?",
                    (now, event_id),
                )
            else:
                conn.execute(
                    "UPDATE event_sessions SET current_round = ?, updated_at = ? WHERE id = ?",
                    (next_round, now, event_id),
                )
                r_id = uuid4().hex[:10]
                round_title = title or f"Round {next_round}"
                conn.execute(
                    "INSERT INTO event_rounds (id, event_id, round_number, title, prompt, status, responses_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (r_id, event_id, next_round, round_title, prompt, "active", "{}", now, now),
                )
            conn.commit()
        return self.get_event(event_id)

    def ask_human(
        self,
        event_id: str,
        question: str,
        options: list[str] | None = None,
        participant_id: str | None = None,
        timeout_seconds: int = 0,
        pending_id: int | None = None,
    ) -> HumanInputRequest:
        ev = self.get_event(event_id)
        req_id = uuid4().hex[:10]
        now = now_iso()
        opts = options or []
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_human_requests (id, event_id, participant_id, question, options_json, timeout_seconds, status, pending_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (req_id, event_id, participant_id, question, json.dumps(opts), timeout_seconds, pending_id, now, now),
            )
            conn.execute("UPDATE event_sessions SET status = 'waiting_human', updated_at = ? WHERE id = ?", (now, event_id))
            conn.commit()
        return HumanInputRequest(
            id=req_id,
            event_id=event_id,
            question=question,
            options=opts,
            participant_id=participant_id,
            timeout_seconds=timeout_seconds,
            status="pending",
            pending_id=pending_id,
            created_at=now,
            updated_at=now,
        )

    def get_human_request(self, request_id: str) -> HumanInputRequest:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM event_human_requests WHERE id = ?", (request_id,)).fetchone()
            if not row:
                raise KeyError(f"No human input request found for id {request_id!r}")
            return HumanInputRequest(
                id=row["id"],
                event_id=row["event_id"],
                question=row["question"],
                options=json.loads(row["options_json"]),
                participant_id=row["participant_id"],
                timeout_seconds=int(row["timeout_seconds"]),
                status=row["status"],
                response=row["response"],
                pending_id=row["pending_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def list_pending_human_requests(self, event_id: str | None = None) -> list[HumanInputRequest]:
        query = "SELECT * FROM event_human_requests WHERE status = 'pending'"
        params: list[Any] = []
        if event_id:
            query += " AND event_id = ?"
            params.append(event_id)
        query += " ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [
                HumanInputRequest(
                    id=r["id"],
                    event_id=r["event_id"],
                    question=r["question"],
                    options=json.loads(r["options_json"]),
                    participant_id=r["participant_id"],
                    timeout_seconds=int(r["timeout_seconds"]),
                    status=r["status"],
                    response=r["response"],
                    pending_id=r["pending_id"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
                for r in rows
            ]

    def respond_human(self, request_id: str, response: str) -> HumanInputRequest:
        req = self.get_human_request(request_id)
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE event_human_requests SET status = 'answered', response = ?, updated_at = ? WHERE id = ?",
                (response, now, request_id),
            )
            # If no more pending requests for this event, restore event status to active
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM event_human_requests WHERE event_id = ? AND status = 'pending' AND id != ?",
                (req.event_id, request_id),
            ).fetchone()[0]
            if pending_count == 0:
                conn.execute(
                    "UPDATE event_sessions SET status = 'active', updated_at = ? WHERE id = ? AND status = 'waiting_human'",
                    (now, req.event_id),
                )
            conn.commit()
        return self.get_human_request(request_id)

    def broadcast_message(
        self,
        event_id: str,
        message: str,
        channel: str = "all",
        round_number: int | None = None,
    ) -> BroadcastMessage:
        ev = self.get_event(event_id)
        b_id = uuid4().hex[:10]
        r_num = ev.current_round if round_number is None else round_number
        now = now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO event_broadcasts (id, event_id, round_number, channel, message, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (b_id, event_id, r_num, channel, message, now),
            )
            conn.commit()
        return BroadcastMessage(id=b_id, event_id=event_id, round_number=r_num, channel=channel, message=message, created_at=now)

    def list_broadcasts(self, event_id: str) -> list[BroadcastMessage]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM event_broadcasts WHERE event_id = ? ORDER BY created_at ASC", (event_id,)).fetchall()
            return [
                BroadcastMessage(
                    id=r["id"],
                    event_id=r["event_id"],
                    round_number=int(r["round_number"]),
                    channel=r["channel"],
                    message=r["message"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def close_event(self, event_id: str, summary: str = "") -> EventSession:
        ev = self.get_event(event_id)
        now = now_iso()
        state = dict(ev.state)
        if summary:
            state["closing_summary"] = summary
        with self._connect() as conn:
            conn.execute(
                "UPDATE event_sessions SET status = 'completed', state_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(state), now, event_id),
            )
            conn.execute(
                "UPDATE event_rounds SET status = 'completed', updated_at = ? WHERE event_id = ? AND status = 'active'",
                (now, event_id),
            )
            conn.commit()
        return self.get_event(event_id)
