from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orac.broker_store import BrokerStore, STALE_SUBAGENT_SECONDS
from orac.models import Board
from orac.session_registry import live_sessions


LOG_SIZE_THRESHOLD_BYTES = 10 * 1024 * 1024
SQLITE_FREE_RATIO_THRESHOLD = 0.25


@dataclass(frozen=True)
class EntropyFinding:
    """One observed maintenance candidate for the idle improvement driver."""

    category: str
    key: str
    observation: str
    goal: str
    acceptance_criteria: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["acceptance_criteria"] = list(self.acceptance_criteria)
        return data


@dataclass(frozen=True)
class GarbageCollectionResult:
    stale_subagents_reaped: int
    stale_session_files_pruned: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def collect_idle_garbage(
    store: BrokerStore,
    root: Path | str,
    *,
    stale_subagent_seconds: int = STALE_SUBAGENT_SECONDS,
) -> GarbageCollectionResult:
    """Run the two deterministic, bounded cleanup operations safe on every idle tick.

    This deliberately does not edit source/docs, rotate logs, or VACUUM SQLite.
    Those operations require a normal reviewed goal. It only releases expired
    runtime leases and session records, both of which already have explicit TTLs.
    """

    sessions_dir = Path(root) / ".orac" / "sessions"
    before = len(list(sessions_dir.glob("*.json"))) if sessions_dir.is_dir() else 0
    live_sessions(root)
    after = len(list(sessions_dir.glob("*.json"))) if sessions_dir.is_dir() else 0
    return GarbageCollectionResult(
        stale_subagents_reaped=store.reap_stale_subagents(
            older_than_seconds=stale_subagent_seconds,
            status="retired",
        ),
        stale_session_files_pruned=max(0, before - after),
    )


def scan_entropy(
    board: Board,
    store: BrokerStore,
    root: Path | str,
    *,
    now: datetime | None = None,
    log_size_threshold: int = LOG_SIZE_THRESHOLD_BYTES,
) -> list[EntropyFinding]:
    """Return stable, evidence-backed maintenance candidates in priority order.

    Detection is read-only. Remediation is intentionally expressed as a regular
    goal so it still passes through Builder, council, verification, and Promoter.
    """

    repo_root = Path(root)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    findings: list[EntropyFinding] = []

    stale_cutoff = now - timedelta(seconds=STALE_SUBAGENT_SECONDS)
    stale = [
        item
        for item in store.list_subagents()
        if item.status in {"proposed", "active"}
        and _parse_time(item.created_at) < stale_cutoff
    ]
    if stale:
        findings.append(
            EntropyFinding(
                category="board",
                key="stale-subagent-reservations",
                observation=f"Observed {len(stale)} stale live subagent reservation(s).",
                goal="Add or verify bounded stale-subagent lease cleanup",
                acceptance_criteria=(
                    "expired proposed/active reservations are retired",
                    "fresh reservations remain live",
                    "tests pass",
                ),
            )
        )

    task_ids = {task.id for task in board.tasks}
    orphans = [task for task in board.tasks if task.parent_id and task.parent_id not in task_ids]
    if orphans:
        findings.append(
            EntropyFinding(
                category="board",
                key="orphaned-child-tasks",
                observation=f"Observed {len(orphans)} child task(s) with no parent on the board.",
                goal="Reconcile orphaned child tasks without discarding their history",
                acceptance_criteria=(
                    "every child references an existing parent or is explicitly retired",
                    "task history is preserved",
                    "tests pass",
                ),
            )
        )

    oversized_logs = sorted(
        (
            path
            for path in (repo_root / ".orac").glob("*.log")
            if path.is_file() and path.stat().st_size > log_size_threshold
        ),
        key=lambda path: path.as_posix(),
    )
    if oversized_logs:
        largest = max(path.stat().st_size for path in oversized_logs)
        findings.append(
            EntropyFinding(
                category="runtime",
                key="oversized-connector-logs",
                observation=(
                    f"Observed {len(oversized_logs)} log file(s) above "
                    f"{log_size_threshold} bytes; largest is {largest} bytes."
                ),
                goal="Implement bounded connector-log rotation that preserves the recent tail",
                acceptance_criteria=(
                    "logs above the declared threshold rotate atomically",
                    "the recent log tail is preserved",
                    "tests pass",
                ),
            )
        )

    # An unacknowledged review is operator work, not code entropy. The notify,
    # status, UI, and chat surfaces already report its exact count and provide
    # ack/rollback actions. Turning a non-empty queue into a self-improvement
    # goal recursively creates redundant "improve the summary" builds while the
    # real action remains simply for the operator to review it.

    free_ratio = _sqlite_free_ratio(repo_root / ".orac" / "broker.db")
    if free_ratio is not None and free_ratio >= SQLITE_FREE_RATIO_THRESHOLD:
        findings.append(
            EntropyFinding(
                category="runtime",
                key="sqlite-fragmentation",
                observation=f"Observed SQLite freelist ratio {free_ratio:.1%}.",
                goal="Add an idle-window SQLite compaction maintenance action",
                acceptance_criteria=(
                    "compaction runs only in an explicit idle maintenance action",
                    "database integrity is checked afterward",
                    "tests pass",
                ),
            )
        )

    return findings


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _sqlite_free_ratio(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(path) as conn:
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            free_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    except sqlite3.Error:
        return None
    return (free_count / page_count) if page_count else 0.0
