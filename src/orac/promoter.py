from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from orac.models import Board, Task, TaskStatus, now_iso


PROMOTION_DIR = Path(".orac") / "promotions"
_CHECKBOX_FILES = frozenset({"TODO.md", "docs/roadmap.md"})


@dataclass(frozen=True)
class PromotionDigest:
    task_id: str
    created_at: str
    title: str
    summary: str
    completed_children: int
    acceptance_criteria: tuple[str, ...]
    reconciled_docs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["acceptance_criteria"] = list(self.acceptance_criteria)
        data["reconciled_docs"] = list(self.reconciled_docs)
        return data

    def message(self) -> str:
        parts = [f"Completed: {self.title}", self.summary]
        if self.completed_children:
            parts.append(f"Verified slices: {self.completed_children}.")
        if self.reconciled_docs:
            parts.append("Docs reconciled: " + ", ".join(self.reconciled_docs) + ".")
        return "\n".join(part for part in parts if part)


def promote_goal(root: Path | str, board: Board, task: Task) -> PromotionDigest:
    """Complete the Promoter stage for one successfully verified goal.

    The operation is idempotent by task id. Documentation reconciliation is
    exact and opt-in: a task may name checkbox text in ``promoter_checkboxes``;
    the Promoter never guesses which roadmap claim a code change satisfies.
    """

    if task.status is not TaskStatus.DONE:
        raise ValueError("Promoter only accepts DONE goals.")
    existing = promotion_for_task(root, task.id)
    if existing is not None:
        task.metadata["promotion"] = {"created_at": existing.created_at}
        return existing

    reconciled = _reconcile_checkboxes(Path(root), task.metadata.get("promoter_checkboxes"))
    children = [child for child in board.tasks if child.parent_id == task.id]
    completed = [child for child in children if child.status is TaskStatus.DONE]
    digest = PromotionDigest(
        task_id=task.id,
        created_at=now_iso(),
        title=task.title,
        summary=_completion_summary(task),
        completed_children=len(completed),
        acceptance_criteria=tuple(task.acceptance_criteria),
        reconciled_docs=tuple(reconciled),
    )
    path = _promotion_path(root, task.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(digest.to_dict(), indent=2) + "\n", encoding="utf-8")
    task.metadata["promotion"] = {"created_at": digest.created_at, "path": str(path)}
    task.add_log("Promoter", f"Published completion digest at {path}.")
    return digest


def list_promotions(root: Path | str, *, limit: int = 20) -> list[PromotionDigest]:
    directory = Path(root) / PROMOTION_DIR
    if not directory.is_dir():
        return []
    digests: list[PromotionDigest] = []
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            digests.append(_from_dict(data))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    digests.sort(key=lambda item: (item.created_at, item.task_id))
    return digests[-max(0, limit) :]


def promotion_for_task(root: Path | str, task_id: str) -> PromotionDigest | None:
    path = _promotion_path(root, task_id)
    if not path.is_file():
        return None
    try:
        return _from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _completion_summary(task: Task) -> str:
    for entry in reversed(task.work_log):
        if entry.agent in {"Orchestrator", "Intent"} and "done" in entry.message.lower():
            return entry.message
    return "Goal passed its configured verification and reached DONE."


def _promotion_path(root: Path | str, task_id: str) -> Path:
    safe_id = "".join(char for char in task_id if char.isalnum() or char in "-_")
    if not safe_id:
        raise ValueError("Task id has no safe filename characters.")
    return Path(root) / PROMOTION_DIR / f"{safe_id}.json"


def _reconcile_checkboxes(root: Path, raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("promoter_checkboxes must be a list of {path, text} mappings.")
    # Validate and prepare every edit in memory first. A malformed later entry
    # must not leave earlier documentation half-reconciled.
    prepared: dict[Path, tuple[str, str]] = {}
    changed: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each promoter checkbox must be a {path, text} mapping.")
        relative = PurePosixPath(str(item.get("path", "")).replace("\\", "/"))
        path_key = relative.as_posix()
        if path_key not in _CHECKBOX_FILES:
            raise ValueError(
                f"Promoter checkbox path {path_key!r} is not one of {sorted(_CHECKBOX_FILES)}."
            )
        text = str(item.get("text", "")).strip()
        if not text:
            raise ValueError("Promoter checkbox text cannot be empty.")
        path = root / Path(*relative.parts)
        if not path.is_file():
            raise FileNotFoundError(f"Promoter checkbox file does not exist: {path}")
        old = f"- [ ] {text}"
        if path in prepared:
            content = prepared[path][1]
        else:
            content = path.read_text(encoding="utf-8")
        count = content.count(old)
        if count == 0:
            if f"- [x] {text}" in content:
                continue
            raise ValueError(f"Promoter checkbox not found exactly in {path_key}: {text!r}")
        if count > 1:
            raise ValueError(f"Promoter checkbox is ambiguous in {path_key}: {text!r}")
        prepared[path] = (path_key, content.replace(old, f"- [x] {text}"))
        if path_key not in changed:
            changed.append(path_key)
    for path, (_, content) in prepared.items():
        path.write_text(content, encoding="utf-8")
    return changed


def _from_dict(data: dict[str, Any]) -> PromotionDigest:
    return PromotionDigest(
        task_id=str(data["task_id"]),
        created_at=str(data["created_at"]),
        title=str(data["title"]),
        summary=str(data["summary"]),
        completed_children=int(data.get("completed_children", 0)),
        acceptance_criteria=tuple(str(item) for item in data.get("acceptance_criteria", [])),
        reconciled_docs=tuple(str(item) for item in data.get("reconciled_docs", [])),
    )
