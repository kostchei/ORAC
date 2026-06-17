from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from orac.models import Task, now_iso

# A Hermes-inspired knowledge layer: persistent memory plus self-improving
# skills, both stored as plain Markdown on disk under .orac/. Nothing here is a
# proprietary format and nothing leaves the machine — a session reads what
# earlier sessions learned, and a session that solves a multi-step task writes a
# reusable skill so the next one starts ahead.
#
# The two halves mirror Nous Research's Hermes Agent (hermes-agent.org):
#   - MEMORY.md / USER.md: a small, char-capped snapshot injected at session
#     start (durable facts the agent curates with add/replace/remove).
#   - SKILL.md files: portable procedures captured from experience, matched to a
#     task and injected into the agent's prompt, version-bumped when re-learned.
#
# It is deliberately offline and dependency-light, in ORAC's house style: skills
# are synthesised deterministically from a session's own transcript (the agent
# writing from experience), not by a second speculative model call.

# Hermes caps each memory file so the snapshot stays a focused brief, not a log.
MEMORY_CHAR_LIMIT = 2200  # ~800 tokens of environment / workflow facts
USER_CHAR_LIMIT = 1375  # ~500 tokens of user identity / preferences

# A skill is only worth capturing once a task took real work. Hermes uses a
# 5+ tool-call threshold; below it the "procedure" is too thin to reuse.
SKILL_MIN_TOOL_CALLS = 5
# How many matched skills to inject into a single prompt, and how much memory.
MAX_INJECTED_SKILLS = 3

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WORD_RE = re.compile(r"[a-z0-9]+")
# English-ish stopwords dropped before keyword matching, so a skill matches on
# what a task is about rather than on its connective tissue.
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have in into is it its of on or
    that the their this to was were will with you your build builds building
    create creates make makes add adds change changes update updates task
    """.split()
)


def _keywords(text: str) -> set[str]:
    return {
        word
        for word in _WORD_RE.findall(text.lower())
        if len(word) > 2 and word not in _STOPWORDS
    }


def _slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "skill"


# --------------------------------------------------------------------------- #
# Persistent memory (MEMORY.md + USER.md)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MemoryWriteResult:
    ok: bool
    message: str
    # When a write overflows the cap, the agent is shown the current entries so
    # it can consolidate (the Hermes capacity-management contract).
    overflow: bool = False
    current: str = ""


class MemoryStore:
    """Two small, char-capped Markdown files the agent curates across sessions.

    MEMORY.md holds environment facts, conventions, and techniques that worked;
    USER.md holds who the operator is and how they like to work. Both are read
    as a frozen snapshot at session start (there is no read *action* — the text
    is simply injected into the prompt), and written with add / replace / remove.
    """

    TARGETS = ("memory", "user")

    def __init__(self, root: Path | str = ".") -> None:
        self.dir = Path(root) / ".orac" / "memory"
        self.memory_path = self.dir / "MEMORY.md"
        self.user_path = self.dir / "USER.md"

    def _path(self, target: str) -> Path:
        if target == "memory":
            return self.memory_path
        if target == "user":
            return self.user_path
        raise ValueError(f"Unknown memory target {target!r}; use one of {self.TARGETS}.")

    def _limit(self, target: str) -> int:
        return MEMORY_CHAR_LIMIT if target == "memory" else USER_CHAR_LIMIT

    def read(self, target: str) -> str:
        path = self._path(target)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _write(self, target: str, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(target).write_text(text.strip() + "\n" if text.strip() else "", encoding="utf-8")

    def add(self, target: str, entry: str) -> MemoryWriteResult:
        entry = entry.strip()
        if not entry:
            return MemoryWriteResult(False, "Refused to add an empty memory entry.")
        current = self.read(target)
        line = entry if entry.startswith("- ") else f"- {entry}"
        candidate = f"{current}\n{line}".strip() if current else line
        limit = self._limit(target)
        if len(candidate) > limit:
            return MemoryWriteResult(
                False,
                f"{target} memory is full ({len(current)}/{limit} chars). "
                "Consolidate existing entries with replace/remove before adding.",
                overflow=True,
                current=current,
            )
        self._write(target, candidate)
        return MemoryWriteResult(True, f"Added to {target} memory.")

    def replace(self, target: str, old_text: str, new_text: str) -> MemoryWriteResult:
        current = self.read(target)
        if old_text not in current:
            return MemoryWriteResult(False, f"{old_text!r} not found in {target} memory.")
        candidate = current.replace(old_text, new_text)
        limit = self._limit(target)
        if len(candidate) > limit:
            return MemoryWriteResult(
                False,
                f"Replacement would exceed the {target} cap ({limit} chars).",
                overflow=True,
                current=current,
            )
        self._write(target, candidate)
        return MemoryWriteResult(True, f"Replaced text in {target} memory.")

    def remove(self, target: str, text: str) -> MemoryWriteResult:
        current = self.read(target)
        if text not in current:
            return MemoryWriteResult(False, f"{text!r} not found in {target} memory.")
        # Drop the matched substring and tidy any line it leaves behind — a bare
        # bullet marker ("- ") with no content is dropped, not kept as noise.
        lines = [
            ln for ln in current.replace(text, "").splitlines() if ln.strip(" -*\t")
        ]
        self._write(target, "\n".join(lines))
        return MemoryWriteResult(True, f"Removed text from {target} memory.")

    def render_for_prompt(self) -> str:
        """The memory snapshot block injected at session start, or "" if empty."""
        blocks: list[str] = []
        user = self.read("user")
        memory = self.read("memory")
        if user:
            blocks.append(f"WHAT YOU KNOW ABOUT THE OPERATOR:\n{user}")
        if memory:
            blocks.append(f"WHAT YOU LEARNED IN EARLIER SESSIONS:\n{memory}")
        return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Skills (SKILL.md)
# --------------------------------------------------------------------------- #


@dataclass
class Skill:
    name: str
    description: str
    when_to_use: str = ""
    procedure: list[str] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    version: str = "1.0.0"
    uses: int = 0
    source_task: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def slug(self) -> str:
        return _slugify(self.name)

    def keywords(self) -> set[str]:
        return _keywords(f"{self.name} {self.description} {self.when_to_use} {' '.join(self.tags)}")

    def to_markdown(self) -> str:
        """Serialise to the portable SKILL.md form: a small frontmatter block of
        ``key: value`` lines, then human-readable sections. Parsed back by
        ``Skill.from_markdown`` — kept simple so no YAML dependency is needed."""
        lines = ["---"]
        lines.append(f"name: {self.name}")
        lines.append(f"description: {self.description}")
        lines.append(f"version: {self.version}")
        lines.append(f"tags: [{', '.join(self.tags)}]")
        lines.append(f"uses: {self.uses}")
        if self.source_task:
            lines.append(f"source_task: {self.source_task}")
        lines.append(f"created_at: {self.created_at}")
        lines.append(f"updated_at: {self.updated_at}")
        lines.append("---")
        lines.append("")
        lines.append(f"# {self.name}")
        lines.append("")
        lines.append(self.description)
        if self.when_to_use:
            lines += ["", "## When to use", "", self.when_to_use]
        if self.procedure:
            lines += ["", "## Procedure", ""]
            lines += [f"{i}. {step}" for i, step in enumerate(self.procedure, 1)]
        if self.pitfalls:
            lines += ["", "## Pitfalls", ""]
            lines += [f"- {p}" for p in self.pitfalls]
        return "\n".join(lines).strip() + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "Skill":
        front, body = _split_frontmatter(text)
        sections = _split_sections(body)
        tags_raw = front.get("tags", "").strip().strip("[]")
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        return cls(
            name=front.get("name", "").strip() or "unnamed-skill",
            description=front.get("description", "").strip()
            or sections.get("_intro", "").strip(),
            when_to_use=sections.get("when to use", "").strip(),
            procedure=_numbered_lines(sections.get("procedure", "")),
            pitfalls=_bullet_lines(sections.get("pitfalls", "")),
            tags=tags,
            version=front.get("version", "1.0.0").strip() or "1.0.0",
            uses=int(front.get("uses", "0").strip() or 0),
            source_task=front.get("source_task", "").strip(),
            created_at=front.get("created_at", "").strip() or now_iso(),
            updated_at=front.get("updated_at", "").strip() or now_iso(),
        )


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    text = text.lstrip("﻿").strip()
    if not text.startswith("---"):
        return {}, text
    rest = text[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}, text
    block = rest[:end]
    body = rest[end + 4 :].strip()
    front: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            front[key.strip().lower()] = value.strip()
    return front, body


def _split_sections(body: str) -> dict[str, str]:
    """Group a skill body into its ``## Heading`` sections (lower-cased keys).

    Text before the first heading — the title line and the description — is
    returned under ``_intro``."""
    sections: dict[str, list[str]] = {"_intro": []}
    current = "_intro"
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections.setdefault(current, [])
        elif line.startswith("# "):
            continue  # the title line, not content
        else:
            sections.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _numbered_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(re.sub(r"^\d+\.\s*", "", line))
    return out


def _bullet_lines(text: str) -> list[str]:
    return [line.strip()[2:].strip() for line in text.splitlines() if line.strip().startswith("- ")]


class SkillLibrary:
    """A directory of SKILL.md files the agents learn from and add to.

    ``match`` ranks skills against a task by keyword overlap so only the
    relevant ones are injected; ``capture`` writes (or patches) a skill from a
    finished session's transcript, so the library grows with experience."""

    def __init__(self, root: Path | str = ".") -> None:
        self.dir = Path(root) / ".orac" / "skills"

    def load_all(self) -> list[Skill]:
        if not self.dir.exists():
            return []
        skills: list[Skill] = []
        for path in sorted(self.dir.glob("*.md")):
            try:
                skills.append(Skill.from_markdown(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 — a malformed skill must not break a run
                continue
        return skills

    def get(self, name: str) -> Skill | None:
        path = self.dir / f"{_slugify(name)}.md"
        if not path.exists():
            return None
        return Skill.from_markdown(path.read_text(encoding="utf-8"))

    def save(self, skill: Skill) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{skill.slug}.md"
        path.write_text(skill.to_markdown(), encoding="utf-8")
        return path

    def match(self, task: Task, *, limit: int = MAX_INJECTED_SKILLS) -> list[Skill]:
        query = _keywords(f"{task.title} {task.description}")
        query |= _keywords(" ".join(task.acceptance_criteria))
        if task.work_kind:
            query.add(task.work_kind.lower())
        if not query:
            return []
        scored: list[tuple[float, Skill]] = []
        for skill in self.load_all():
            overlap = query & skill.keywords()
            if not overlap:
                continue
            # Relevance first, then prefer skills proven by repeated use.
            score = len(overlap) + min(skill.uses, 10) * 0.1
            scored.append((score, skill))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [skill for _, skill in scored[:limit]]

    def record_use(self, skill: Skill) -> None:
        """Mark a skill as used once more (the cheap half of 'patched in use')."""
        on_disk = self.get(skill.name) or skill
        on_disk.uses += 1
        on_disk.updated_at = now_iso()
        self.save(on_disk)

    def render_for_prompt(self, skills: list[Skill]) -> str:
        if not skills:
            return ""
        blocks = ["LEARNED SKILLS (from earlier sessions — follow when they fit):"]
        for skill in skills:
            blocks.append(f"\n### {skill.name}")
            if skill.when_to_use:
                blocks.append(f"When: {skill.when_to_use}")
            elif skill.description:
                blocks.append(skill.description)
            for i, step in enumerate(skill.procedure, 1):
                blocks.append(f"{i}. {step}")
            for pitfall in skill.pitfalls:
                blocks.append(f"! {pitfall}")
        return "\n".join(blocks)

    def capture(
        self,
        *,
        name: str,
        description: str,
        when_to_use: str,
        procedure: list[str],
        pitfalls: list[str],
        tags: list[str],
        source_task: str,
    ) -> Skill:
        """Write a new skill, or patch an existing one of the same name.

        Re-learning a skill bumps its minor version and refreshes the procedure
        rather than spawning a duplicate — Hermes's 'skills are patched during
        use when outdated, incomplete, or wrong' applied to capture time."""
        existing = self.get(name)
        if existing is not None:
            existing.description = description or existing.description
            existing.when_to_use = when_to_use or existing.when_to_use
            existing.procedure = procedure or existing.procedure
            existing.pitfalls = sorted(set(existing.pitfalls) | set(pitfalls))
            existing.tags = sorted(set(existing.tags) | set(tags))
            existing.version = _bump_minor(existing.version)
            existing.updated_at = now_iso()
            self.save(existing)
            return existing
        skill = Skill(
            name=name,
            description=description,
            when_to_use=when_to_use,
            procedure=procedure,
            pitfalls=pitfalls,
            tags=tags,
            source_task=source_task,
        )
        self.save(skill)
        return skill


def _bump_minor(version: str) -> str:
    parts = version.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return "1.1.0"
    return f"{major}.{minor + 1}.0"


# --------------------------------------------------------------------------- #
# The learning loop: synthesise a skill from a finished session's transcript
# --------------------------------------------------------------------------- #

_ACTION_RE = re.compile(r"^ACTION \d+: (\S+) (.*)$")
_OBS_RE = re.compile(r"^OBSERVATION \d+ \[([a-z]+)\]: (.*)$")


def count_tool_calls(transcript: list[str]) -> int:
    return sum(1 for line in transcript if _ACTION_RE.match(line))


def synthesise_skill(
    task: Task, transcript: list[str], *, summary: str = ""
) -> Skill | None:
    """Turn the agent's own ACTION/OBSERVATION transcript into a reusable skill.

    Deterministic and offline: the working sequence of *allowed* tool calls
    becomes the procedure, denied/errored steps become pitfalls, and the task's
    goal becomes the trigger. Returns None if the run was too thin to be worth a
    skill (below ``SKILL_MIN_TOOL_CALLS``)."""
    if count_tool_calls(transcript) < SKILL_MIN_TOOL_CALLS:
        return None

    procedure: list[str] = []
    pitfalls: list[str] = []
    pending_tool: str | None = None
    for line in transcript:
        action = _ACTION_RE.match(line)
        if action:
            pending_tool = action.group(1)
            continue
        obs = _OBS_RE.match(line)
        if obs and pending_tool is not None:
            status = obs.group(1)
            if status == "allowed":
                procedure.append(f"Use `{pending_tool}`.")
            else:
                detail = obs.group(2).strip()[:120]
                pitfalls.append(f"`{pending_tool}` came back [{status}]: {detail}")
            pending_tool = None

    if not procedure:
        return None
    # Collapse immediate repeats ("use repo.edit_file" three times in a row) into
    # one step so the procedure reads as a method, not a replayed log.
    procedure = _dedupe_runs(procedure)

    name = _skill_name_for(task)
    tags = [task.work_kind] if task.work_kind else []
    return Skill(
        name=name,
        description=summary.strip() or f"How to: {task.title}".strip(),
        when_to_use=f"A {task.work_kind or 'similar'} task like: {task.title}",
        procedure=procedure,
        pitfalls=pitfalls[:5],
        tags=tags,
        source_task=task.id,
    )


def _dedupe_runs(steps: list[str]) -> list[str]:
    out: list[str] = []
    for step in steps:
        if not out or out[-1] != step:
            out.append(step)
    return out


def _skill_name_for(task: Task) -> str:
    kind = (task.work_kind or "task").strip()
    keys = sorted(_keywords(task.title))[:3]
    topic = "-".join(keys) if keys else _slugify(task.title)[:30]
    return f"{kind}: {topic}".strip()


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #


class KnowledgeBase:
    """Bundles the memory snapshot and skill library for one project root, and
    carries the session-injection / skill-capture policy in one place so callers
    wire a single object into a session."""

    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root)
        self.memory = MemoryStore(root)
        self.skills = SkillLibrary(root)

    def prompt_preamble(self, task: Task) -> tuple[str, list[Skill]]:
        """The memory + matched-skill block to prepend to a session prompt, and
        the skills that were injected (so the caller can record their use)."""
        matched = self.skills.match(task)
        blocks = [
            block
            for block in (self.memory.render_for_prompt(), self.skills.render_for_prompt(matched))
            if block
        ]
        return ("\n\n".join(blocks), matched)

    def capture_from_session(
        self, task: Task, transcript: list[str], *, summary: str = ""
    ) -> Skill | None:
        """Capture a skill from a finished session if it earned one."""
        skill = synthesise_skill(task, transcript, summary=summary)
        if skill is None:
            return None
        return self.skills.capture(
            name=skill.name,
            description=skill.description,
            when_to_use=skill.when_to_use,
            procedure=skill.procedure,
            pitfalls=skill.pitfalls,
            tags=skill.tags,
            source_task=skill.source_task,
        )
