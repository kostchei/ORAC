from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from orac.adapters import Adapter
from orac.models import CapabilityRequest, now_iso
from orac.tooling import ToolResult

# The skill library: a closed learning loop store (the agentskills.io / Hermes
# pattern) done the ORAC way — every mutation routes through the broker and is
# governed by name like any other adapter (catalog -> grant -> risk_class ->
# council -> approval_mode). Two invariants set this apart from a naive port:
#
#   1. Containment. Every path resolves inside ``.orac/skills``; an arg that
#      escapes the root raises (no fallback, no silent clamp) — the same guard
#      the Builder's code adapters use for the repo.
#   2. Engineered reversibility. The repo's code tools are reversible because the
#      Builder works checkpoint-first (branch + commit). Skills live under the
#      gitignored ``.orac`` tree, so there is no git checkpoint to fall back on.
#      We earn the same reversibility deliberately: every overwrite snapshots the
#      prior file under ``.history`` first, and a "delete" is an *archive* (move
#      under ``.archive``), never an ``rmtree``. There is no hard-delete tool.
#
# This is the difference that lets the write tools be classified REVERSIBLE/LOCAL
# (auto + audit) honestly in policy.py rather than parking every skill edit.

SKILL_FILE = "SKILL.md"
_HISTORY_DIR = ".history"
_ARCHIVE_DIR = ".archive"
_RESERVED = (_HISTORY_DIR, _ARCHIVE_DIR)

SKILL_READ_TOOLS = frozenset({"skill.list", "skill.view"})
SKILL_WRITE_TOOLS = frozenset(
    {"skill.create", "skill.edit", "skill.patch", "skill.write_file", "skill.archive"}
)
SKILL_TOOLS = SKILL_READ_TOOLS | SKILL_WRITE_TOOLS


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Split leading ``---`` YAML-ish frontmatter from the body.

    Only the *first* fenced block at the very top is treated as frontmatter, and
    parsing stops at the first closing ``---`` — so a ``---`` rule inside the body
    is left untouched (the naive ``split('---', 2)`` port corrupts such files).
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta: dict[str, str] = {}
            for raw in lines[1:i]:
                line = raw.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip().strip("\"'")
            body = "\n".join(lines[i + 1 :]).strip()
            return meta, body
    return {}, content


def _stamp() -> str:
    """A filesystem-safe, collision-resistant version stamp."""
    return now_iso().replace(":", "").replace("-", "") + "-" + uuid4().hex[:6]


@dataclass(frozen=True)
class SkillAdapterSet:
    """Skill adapters confined to a single ``.orac/skills`` root.

    Every path argument is resolved and checked to fall inside the root; anything
    outside raises. Mirrors :class:`code_adapters.CodeAdapterSet` so the broker
    can register either the same way.
    """

    skills_root: Path

    def adapters(self) -> dict[str, Adapter]:
        return {
            "skill.list": self.list_skills,
            "skill.view": self.view,
            "skill.create": self.create,
            "skill.edit": self.edit,
            "skill.patch": self.patch,
            "skill.write_file": self.write_file,
            "skill.archive": self.archive,
        }

    # --- path guards ------------------------------------------------------

    def _root(self) -> Path:
        self.skills_root.mkdir(parents=True, exist_ok=True)
        return self.skills_root.resolve()

    def _resolve(self, *parts: str) -> Path:
        """Join ``parts`` under the skills root and refuse anything that escapes."""
        root = self._root()
        candidate = root
        for part in parts:
            candidate = candidate / part
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise PermissionError(
                f"Path {resolved} is outside the skills root {root}."
            )
        return resolved

    def _rel_parts(self, skill_dir: Path) -> tuple[str, ...]:
        return skill_dir.relative_to(self._root()).parts

    def _iter_skill_files(self):
        root = self._root()
        for skill_md in sorted(root.rglob(SKILL_FILE)):
            if any(reserved in skill_md.parts for reserved in _RESERVED):
                continue  # never surface history/archive copies as live skills
            yield skill_md

    def _find(self, name: str) -> Path:
        for skill_md in self._iter_skill_files():
            meta, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            if meta.get("name") == name or skill_md.parent.name == name:
                return skill_md.parent
        raise FileNotFoundError(f"Skill {name!r} not found.")

    def _snapshot(self, path: Path) -> None:
        """Copy ``path`` under ``.history`` before it is overwritten.

        The non-git equivalent of the Builder's checkpoint-first write: a prior
        version is always recoverable, so an overwrite stays reversible-local.
        """
        if not path.exists():
            return
        rel = path.relative_to(self._root())
        dest = self._root() / _HISTORY_DIR / rel.parent / f"{path.name}.{_stamp()}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    # --- read adapters ----------------------------------------------------

    def list_skills(self, req: CapabilityRequest) -> ToolResult:
        root = self._root()
        category = req.args.get("category", "")
        skills: list[dict[str, str]] = []
        for skill_md in self._iter_skill_files():
            rel = skill_md.parent.relative_to(root)
            skill_name = rel.parts[-1]
            skill_category = "/".join(rel.parts[:-1])
            meta, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            if category and category not in (skill_category, skill_name):
                continue
            skills.append(
                {
                    "name": meta.get("name", skill_name),
                    "description": meta.get("description", ""),
                    "category": skill_category,
                    "path": rel.as_posix(),
                }
            )
        return ToolResult(
            "skill.list",
            f"Found {len(skills)} skill(s).",
            {"skills": skills, "count": len(skills)},
        )

    def view(self, req: CapabilityRequest) -> ToolResult:
        name = req.args["name"]
        skill_dir = self._find(name)
        file_path = req.args.get("file_path", "")
        if file_path:
            target = self._resolve(*self._rel_parts(skill_dir), *Path(file_path).parts)
            if not target.is_file():
                raise FileNotFoundError(
                    f"skill.view: no file {file_path!r} in skill {name!r}."
                )
            return ToolResult(
                "skill.view",
                f"Read {file_path!r} from skill {name!r}.",
                {
                    "name": name,
                    "file_path": file_path,
                    "content": target.read_text(encoding="utf-8"),
                },
            )
        content = (skill_dir / SKILL_FILE).read_text(encoding="utf-8")
        meta, _ = _parse_frontmatter(content)
        linked = [
            p.relative_to(skill_dir).as_posix()
            for p in sorted(skill_dir.rglob("*"))
            if p.is_file() and p.name != SKILL_FILE
        ]
        return ToolResult(
            "skill.view",
            f"Loaded skill {name!r}.",
            {
                "name": meta.get("name", name),
                "description": meta.get("description", ""),
                "content": content,
                "linked_files": linked,
            },
        )

    # --- write adapters (reversible: snapshot-first / archive-not-delete) --

    def create(self, req: CapabilityRequest) -> ToolResult:
        name = req.args["name"]
        content = req.args["content"]
        if not content.strip():
            raise ValueError("skill.create requires non-empty 'content'.")
        category = req.args.get("category", "")
        parts = ([category] if category else []) + [name]
        skill_dir = self._resolve(*parts)
        skill_file = skill_dir / SKILL_FILE
        if skill_file.exists():
            raise FileExistsError(f"Skill {name!r} already exists at {skill_file}.")
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(content, encoding="utf-8")
        return ToolResult(
            "skill.create",
            f"Created skill {name!r}.",
            {"name": name, "path": skill_file.relative_to(self._root()).as_posix()},
        )

    def edit(self, req: CapabilityRequest) -> ToolResult:
        name = req.args["name"]
        content = req.args["content"]
        if not content.strip():
            raise ValueError("skill.edit requires non-empty 'content'.")
        target = self._find(name) / SKILL_FILE
        self._snapshot(target)
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            "skill.edit",
            f"Rewrote {SKILL_FILE} for skill {name!r} (prior version snapshotted).",
            {"name": name},
        )

    def patch(self, req: CapabilityRequest) -> ToolResult:
        """Replace one exact, unique occurrence of ``old`` with ``new``.

        The surgical alternative to a full rewrite, fail-closed like
        ``repo.edit_file``: ``old`` must occur exactly once (unless
        ``replace_all``), or nothing is written.
        """
        name = req.args["name"]
        old = req.args["old"]
        new = req.args["new"]
        if old == new:
            raise ValueError("skill.patch: 'old' and 'new' are identical; no edit.")
        skill_dir = self._find(name)
        file_path = req.args.get("file_path", "")
        target = (
            self._resolve(*self._rel_parts(skill_dir), *Path(file_path).parts)
            if file_path
            else skill_dir / SKILL_FILE
        )
        if not target.is_file():
            raise FileNotFoundError(
                f"skill.patch: no file {file_path or SKILL_FILE!r} in skill {name!r}."
            )
        text = target.read_text(encoding="utf-8")
        replace_all = bool(req.args.get("replace_all", False))
        count = text.count(old)
        if count == 0:
            raise ValueError(f"skill.patch: 'old' text not found in skill {name!r}.")
        if count > 1 and not replace_all:
            raise ValueError(
                f"skill.patch: 'old' occurs {count}x in skill {name!r}; it must be "
                "unique. Add surrounding context or pass replace_all=true."
            )
        self._snapshot(target)
        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        target.write_text(new_text, encoding="utf-8")
        return ToolResult(
            "skill.patch",
            f"Patched {file_path or SKILL_FILE} in skill {name!r}.",
            {"name": name, "file_path": file_path or SKILL_FILE},
        )

    def write_file(self, req: CapabilityRequest) -> ToolResult:
        """Write a supporting file alongside a skill's SKILL.md.

        SKILL.md itself is off-limits here — use ``skill.edit``/``skill.patch``
        so the frontmatter path stays a single, intentional surface.
        """
        name = req.args["name"]
        file_path = req.args["file_path"]
        file_content = req.args["file_content"]
        if Path(file_path).name == SKILL_FILE:
            raise ValueError(
                "skill.write_file cannot write SKILL.md; use skill.edit/skill.patch."
            )
        skill_dir = self._find(name)
        target = self._resolve(*self._rel_parts(skill_dir), *Path(file_path).parts)
        self._snapshot(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_content, encoding="utf-8")
        return ToolResult(
            "skill.write_file",
            f"Wrote {file_path!r} in skill {name!r}.",
            {"name": name, "file_path": file_path},
        )

    def archive(self, req: CapabilityRequest) -> ToolResult:
        """Retire a skill by moving it under ``.archive`` — never delete it.

        The Hermes curator's load-bearing invariant: removal is recoverable. A
        consolidated/superseded skill is set aside, not destroyed, so a wrong
        call costs a restore, not the work.
        """
        name = req.args["name"]
        skill_dir = self._find(name)
        dest = self._root() / _ARCHIVE_DIR / f"{skill_dir.name}.{_stamp()}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(skill_dir), str(dest))
        note = req.args.get("absorbed_into", "")
        message = f"Archived skill {name!r} (recoverable under {_ARCHIVE_DIR})."
        if note:
            message += f" Absorbed into {note!r}."
        return ToolResult(
            "skill.archive",
            message,
            {"name": name, "archived_to": dest.relative_to(self._root()).as_posix()},
        )


def skills_adapters_for(repo_root: Path | str) -> dict[str, Adapter]:
    """Skill adapters for the ``.orac/skills`` tree under ``repo_root``."""
    root = Path(repo_root).resolve() / ".orac" / "skills"
    return SkillAdapterSet(root).adapters()
