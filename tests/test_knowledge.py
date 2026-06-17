from __future__ import annotations

from pathlib import Path

import pytest

from orac.knowledge import (
    MEMORY_CHAR_LIMIT,
    SKILL_MIN_TOOL_CALLS,
    KnowledgeBase,
    MemoryStore,
    Skill,
    SkillLibrary,
    count_tool_calls,
    synthesise_skill,
)
from orac.models import Task


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #


def test_memory_add_read_roundtrip(tmp_path: Path) -> None:
    mem = MemoryStore(tmp_path)
    assert mem.read("memory") == ""
    assert mem.add("memory", "Tests live under tests/ and run with pytest").ok
    assert "pytest" in mem.read("memory")
    # Stored under .orac/memory as plain Markdown.
    assert (tmp_path / ".orac" / "memory" / "MEMORY.md").exists()


def test_memory_add_is_bulleted_and_appends(tmp_path: Path) -> None:
    mem = MemoryStore(tmp_path)
    mem.add("user", "Prefers concise replies")
    mem.add("user", "Works in the Pacific timezone")
    text = mem.read("user")
    assert text.count("- ") == 2
    assert "concise" in text and "Pacific" in text


def test_memory_respects_char_cap(tmp_path: Path) -> None:
    mem = MemoryStore(tmp_path)
    assert mem.add("memory", "x" * (MEMORY_CHAR_LIMIT - 10)).ok
    overflow = mem.add("memory", "y" * 100)
    assert not overflow.ok
    assert overflow.overflow
    assert overflow.current  # the agent is shown what to consolidate


def test_memory_replace_and_remove(tmp_path: Path) -> None:
    mem = MemoryStore(tmp_path)
    mem.add("memory", "Build with make")
    assert mem.replace("memory", "make", "uv").ok
    assert "uv" in mem.read("memory")
    assert not mem.replace("memory", "absent", "x").ok
    assert mem.remove("memory", "Build with uv").ok
    assert mem.read("memory") == ""


def test_memory_unknown_target_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        MemoryStore(tmp_path).read("nonsense")


def test_memory_render_orders_user_then_memory(tmp_path: Path) -> None:
    mem = MemoryStore(tmp_path)
    mem.add("memory", "env fact")
    mem.add("user", "operator name is Ada")
    rendered = mem.render_for_prompt()
    assert rendered.index("OPERATOR") < rendered.index("EARLIER SESSIONS")


# --------------------------------------------------------------------------- #
# Skill serialization
# --------------------------------------------------------------------------- #


def test_skill_markdown_roundtrip(tmp_path: Path) -> None:
    skill = Skill(
        name="code: add a cli command",
        description="Wire a new subcommand into the argparse CLI.",
        when_to_use="Adding an orac subcommand",
        procedure=["Add the parser", "Add the handler", "Wire dispatch in main"],
        pitfalls=["`repo.edit_file` came back [denied]: out of scope"],
        tags=["code"],
        uses=3,
        source_task="abc123",
    )
    text = skill.to_markdown()
    assert text.startswith("---")
    back = Skill.from_markdown(text)
    assert back.name == skill.name
    assert back.description == skill.description
    assert back.when_to_use == skill.when_to_use
    assert back.procedure == skill.procedure
    assert back.pitfalls == skill.pitfalls
    assert back.tags == ["code"]
    assert back.uses == 3
    assert back.source_task == "abc123"


def test_skill_from_markdown_without_frontmatter() -> None:
    skill = Skill.from_markdown("# A title\n\nJust a description, no frontmatter.")
    assert skill.name == "unnamed-skill"
    assert "description" in skill.description


# --------------------------------------------------------------------------- #
# Skill library: save / match / use
# --------------------------------------------------------------------------- #


def test_library_save_and_get(tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    lib.save(Skill(name="Code: Run Tests", description="how to run the suite"))
    got = lib.get("code: run tests")  # slug match, case-insensitive
    assert got is not None
    assert got.description == "how to run the suite"


def test_library_match_ranks_by_keyword_overlap(tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    lib.save(Skill(name="parser skill", description="argparse subcommand parser cli", tags=["code"]))
    lib.save(Skill(name="browser skill", description="navigate a webpage with cdp", tags=["code"]))
    task = Task(title="Add a new parser subcommand to the cli", work_kind="code")
    matched = lib.match(task)
    assert matched
    assert matched[0].name == "parser skill"


def test_library_match_empty_when_no_overlap(tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    lib.save(Skill(name="unrelated", description="quantum chromodynamics"))
    task = Task(title="paint the fence", description="white picket")
    assert lib.match(task) == []


def test_record_use_increments_and_persists(tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    skill = lib.save(Skill(name="s", description="d")) and lib.get("s")
    assert skill is not None
    lib.record_use(skill)
    lib.record_use(skill)
    assert lib.get("s").uses == 2


def test_capture_patches_existing_skill_and_bumps_version(tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    first = lib.capture(
        name="code: thing", description="v1", when_to_use="w", procedure=["a"],
        pitfalls=["p1"], tags=["code"], source_task="t1",
    )
    assert first.version == "1.0.0"
    second = lib.capture(
        name="code: thing", description="v2", when_to_use="w2", procedure=["a", "b"],
        pitfalls=["p2"], tags=["code", "extra"], source_task="t2",
    )
    assert second.version == "1.1.0"
    assert second.procedure == ["a", "b"]
    assert set(second.pitfalls) == {"p1", "p2"}  # pitfalls accumulate
    # Only one file on disk — patched, not duplicated.
    assert len(list((tmp_path / ".orac" / "skills").glob("*.md"))) == 1


# --------------------------------------------------------------------------- #
# The learning loop: synthesise a skill from a transcript
# --------------------------------------------------------------------------- #


def _transcript(n_allowed: int) -> list[str]:
    lines: list[str] = []
    for i in range(1, n_allowed + 1):
        lines.append(f"ACTION {i}: repo.edit_file {{\"path\": \"f{i}\"}}")
        lines.append(f"OBSERVATION {i} [allowed]: ok")
    return lines


def test_count_tool_calls() -> None:
    assert count_tool_calls(_transcript(3)) == 3
    assert count_tool_calls([]) == 0


def test_synthesise_skill_below_threshold_returns_none() -> None:
    task = Task(title="tiny change", work_kind="code")
    assert synthesise_skill(task, _transcript(SKILL_MIN_TOOL_CALLS - 1)) is None


def test_synthesise_skill_builds_procedure_and_pitfalls() -> None:
    task = Task(title="add parser subcommand for memory", work_kind="code")
    transcript = [
        "ACTION 1: task_reader {}",
        "OBSERVATION 1 [allowed]: read",
        "ACTION 2: repo.write_file {}",
        "OBSERVATION 2 [denied]: out of scope",
        "ACTION 3: repo.edit_file {}",
        "OBSERVATION 3 [allowed]: ok",
        "ACTION 4: repo.edit_file {}",
        "OBSERVATION 4 [allowed]: ok",
        "ACTION 5: repo.run_tests {}",
        "OBSERVATION 5 [allowed]: passed",
    ]
    skill = synthesise_skill(task, transcript, summary="wired a subcommand")
    assert skill is not None
    assert skill.source_task == task.id
    assert skill.tags == ["code"]
    assert skill.description == "wired a subcommand"
    # Allowed tools become procedure; immediate repeats collapse to one step.
    assert any("repo.edit_file" in step for step in skill.procedure)
    assert skill.procedure.count("Use `repo.edit_file`.") == 1
    # The denied write is captured as a pitfall, not a step.
    assert any("denied" in p for p in skill.pitfalls)


# --------------------------------------------------------------------------- #
# KnowledgeBase facade
# --------------------------------------------------------------------------- #


def test_knowledgebase_preamble_includes_memory_and_skill(tmp_path: Path) -> None:
    kb = KnowledgeBase(tmp_path)
    kb.memory.add("memory", "repo uses pytest")
    kb.skills.save(Skill(name="code: parser", description="cli parser subcommand", tags=["code"]))
    task = Task(title="add a parser subcommand", work_kind="code")
    block, matched = kb.prompt_preamble(task)
    assert "pytest" in block
    assert "LEARNED SKILLS" in block
    assert len(matched) == 1


def test_knowledgebase_capture_from_session(tmp_path: Path) -> None:
    kb = KnowledgeBase(tmp_path)
    task = Task(title="build a memory module", work_kind="code")
    captured = kb.capture_from_session(task, _transcript(SKILL_MIN_TOOL_CALLS), summary="done")
    assert captured is not None
    assert kb.skills.get(captured.name) is not None


def test_knowledgebase_capture_skips_thin_session(tmp_path: Path) -> None:
    kb = KnowledgeBase(tmp_path)
    task = Task(title="trivial", work_kind="code")
    assert kb.capture_from_session(task, _transcript(1)) is None
    assert kb.skills.load_all() == []
