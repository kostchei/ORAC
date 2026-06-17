from __future__ import annotations

import pytest

from orac.models import CapabilityRequest
from orac.skills_adapters import (
    SKILL_READ_TOOLS,
    SKILL_WRITE_TOOLS,
    SkillAdapterSet,
    _parse_frontmatter,
    skills_adapters_for,
)


def _adapters(tmp_path) -> SkillAdapterSet:
    return SkillAdapterSet(tmp_path / ".orac" / "skills")


def _req(tool: str, **args) -> CapabilityRequest:
    return CapabilityRequest(agent="Builder", tool=tool, task_id="t1", args=args)


_FRONTMATTER = (
    "---\n"
    'name: greet\n'
    'description: "say hello"\n'
    "---\n"
    "# Greet\n\nStep 1 — wave.\n"
)


def test_create_then_view_and_list(tmp_path) -> None:
    a = _adapters(tmp_path)
    res = a.create(_req("skill.create", name="greet", content=_FRONTMATTER, category="social"))
    assert res.name == "skill.create"

    listed = a.list_skills(_req("skill.list"))
    assert listed.data["count"] == 1
    entry = listed.data["skills"][0]
    assert entry["name"] == "greet"
    assert entry["description"] == "say hello"
    assert entry["category"] == "social"

    viewed = a.view(_req("skill.view", name="greet"))
    assert viewed.data["description"] == "say hello"
    assert "wave" in viewed.data["content"]


def test_create_rejects_duplicate(tmp_path) -> None:
    a = _adapters(tmp_path)
    a.create(_req("skill.create", name="greet", content=_FRONTMATTER))
    with pytest.raises(FileExistsError):
        a.create(_req("skill.create", name="greet", content=_FRONTMATTER))


def test_create_rejects_empty_content(tmp_path) -> None:
    a = _adapters(tmp_path)
    with pytest.raises(ValueError):
        a.create(_req("skill.create", name="empty", content="   "))


def test_edit_snapshots_prior_version(tmp_path) -> None:
    a = _adapters(tmp_path)
    a.create(_req("skill.create", name="greet", content=_FRONTMATTER))
    a.edit(_req("skill.edit", name="greet", content="---\nname: greet\n---\nv2 body\n"))

    assert "v2 body" in a.view(_req("skill.view", name="greet")).data["content"]
    history = list((tmp_path / ".orac" / "skills" / ".history").rglob("SKILL.md.*"))
    assert len(history) == 1
    assert "wave" in history[0].read_text(encoding="utf-8")  # the prior version


def test_patch_requires_unique_match(tmp_path) -> None:
    a = _adapters(tmp_path)
    a.create(_req("skill.create", name="dup", content="---\nname: dup\n---\nx and x\n"))
    with pytest.raises(ValueError):
        a.patch(_req("skill.patch", name="dup", old="x", new="y"))
    # replace_all lifts the uniqueness requirement
    a.patch(_req("skill.patch", name="dup", old="x", new="y", replace_all=True))
    assert "y and y" in a.view(_req("skill.view", name="dup")).data["content"]


def test_patch_identical_old_new_rejected(tmp_path) -> None:
    a = _adapters(tmp_path)
    a.create(_req("skill.create", name="s", content=_FRONTMATTER))
    with pytest.raises(ValueError):
        a.patch(_req("skill.patch", name="s", old="wave", new="wave"))


def test_write_file_cannot_target_skill_md(tmp_path) -> None:
    a = _adapters(tmp_path)
    a.create(_req("skill.create", name="s", content=_FRONTMATTER))
    with pytest.raises(ValueError):
        a.write_file(_req("skill.write_file", name="s", file_path="SKILL.md", file_content="x"))


def test_write_file_adds_supporting_file_listed_in_view(tmp_path) -> None:
    a = _adapters(tmp_path)
    a.create(_req("skill.create", name="s", content=_FRONTMATTER))
    a.write_file(_req("skill.write_file", name="s", file_path="ref/notes.md", file_content="hi"))
    viewed = a.view(_req("skill.view", name="s"))
    assert "ref/notes.md" in viewed.data["linked_files"]
    one = a.view(_req("skill.view", name="s", file_path="ref/notes.md"))
    assert one.data["content"] == "hi"


def test_archive_is_recoverable_not_a_delete(tmp_path) -> None:
    a = _adapters(tmp_path)
    a.create(_req("skill.create", name="old", content=_FRONTMATTER))
    res = a.archive(_req("skill.archive", name="old", absorbed_into="new"))

    # gone from the live library...
    assert a.list_skills(_req("skill.list")).data["count"] == 0
    with pytest.raises(FileNotFoundError):
        a.view(_req("skill.view", name="old"))
    # ...but the bytes survive under .archive
    archived = tmp_path / ".orac" / "skills" / ".archive" / res.data["archived_to"].split("/")[-1]
    assert (archived / "SKILL.md").is_file()


def test_path_containment_blocks_escape(tmp_path) -> None:
    a = _adapters(tmp_path)
    a.create(_req("skill.create", name="s", content=_FRONTMATTER))
    with pytest.raises(PermissionError):
        a.write_file(
            _req("skill.write_file", name="s", file_path="../../escape.txt", file_content="x")
        )
    assert not (tmp_path / "escape.txt").exists()


def test_view_missing_skill_raises(tmp_path) -> None:
    a = _adapters(tmp_path)
    with pytest.raises(FileNotFoundError):
        a.view(_req("skill.view", name="ghost"))


def test_frontmatter_ignores_rule_in_body() -> None:
    meta, body = _parse_frontmatter("---\nname: n\n---\nintro\n\n---\n\nmore\n")
    assert meta == {"name": "n"}
    assert "---" in body  # the body's horizontal rule is preserved, not eaten


def test_factory_roots_under_dot_orac(tmp_path) -> None:
    adapters = skills_adapters_for(tmp_path)
    assert set(adapters) == SKILL_READ_TOOLS | SKILL_WRITE_TOOLS
