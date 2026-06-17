from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from orac.tooling import ToolResult

import json
import shutil
from pathlib import Path
from typing import Any, Optional

from orac.models import Task

SKILLS_DIR = Path(".orac/skills")

def _get_skills_dir() -> Path:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return SKILLS_DIR

def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    frontmatter_text = parts[1]
    body = parts[2].strip()

    metadata = {}
    for line in frontmatter_text.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"\'')

    return metadata, body


def skills_list(task: Task, agent: str, category: str = "") -> tuple[str, str, dict[str, Any]]:
    skills_dir = _get_skills_dir()
    skills = []

    for skill_path in skills_dir.rglob("SKILL.md"):
        rel_path = skill_path.relative_to(skills_dir)
        # category could be the parent folder if not at root
        parts = rel_path.parts
        skill_name = parts[-2] if len(parts) > 1 else parts[0]
        skill_category = parts[-3] if len(parts) > 2 else ""

        if category and skill_category != category and skill_name != category:
            continue

        content = skill_path.read_text(encoding="utf-8")
        metadata, _ = _parse_frontmatter(content)

        name = metadata.get("name", skill_name)
        description = metadata.get("description", "")

        skills.append({
            "name": name,
            "description": description,
            "category": skill_category,
            "path": str(rel_path.parent)
        })

    message = f"Found {len(skills)} skill(s)."
    task.add_log(agent, message)
    return ("skills_list", message, {"skills": skills, "count": len(skills)})


def skill_view(task: Task, agent: str, name: str, file_path: str = "") -> tuple[str, str, dict[str, Any]]:
    skills_dir = _get_skills_dir()

    skill_dir = None
    for p in skills_dir.rglob("SKILL.md"):
        content = p.read_text(encoding="utf-8")
        metadata, _ = _parse_frontmatter(content)
        if metadata.get("name") == name or p.parent.name == name:
            skill_dir = p.parent
            break

    if not skill_dir:
        raise ValueError(f"Skill '{name}' not found.")

    if file_path:
        target_path = skill_dir / file_path
        if not target_path.is_file():
            raise FileNotFoundError(f"File '{file_path}' not found in skill '{name}'.")
        content = target_path.read_text(encoding="utf-8")
        message = f"Loaded file '{file_path}' for skill '{name}'."
        task.add_log(agent, message)
        return ("skill_view", message, {"name": name, "file_path": file_path, "content": content})

    # Load main skill
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    metadata, body = _parse_frontmatter(content)

    linked_files = []
    for p in skill_dir.rglob("*"):
        if p.is_file() and p.name != "SKILL.md":
            linked_files.append(str(p.relative_to(skill_dir)))

    message = f"Loaded skill '{name}'."
    task.add_log(agent, message)
    return ("skill_view", message, {
        "name": metadata.get("name", name),
        "description": metadata.get("description", ""),
        "content": content,
        "linked_files": linked_files
    })


def skill_manage(
    task: Task,
    agent: str,
    action: str,
    name: str,
    content: str = "",
    category: str = "",
    file_path: str = "",
    file_content: str = "",
    old_string: str = "",
    new_string: str = "",
    replace_all: bool = False,
    absorbed_into: str = ""
) -> tuple[str, str, dict[str, Any]]:
    skills_dir = _get_skills_dir()

    def _find_skill_dir(n: str) -> Optional[Path]:
        for p in skills_dir.rglob("SKILL.md"):
            md, _ = _parse_frontmatter(p.read_text(encoding="utf-8"))
            if md.get("name") == n or p.parent.name == n:
                return p.parent
        return None

    if action == "create":
        if not content:
            raise ValueError("content is required for 'create'.")

        target_dir = skills_dir / category / name if category else skills_dir / name
        target_dir.mkdir(parents=True, exist_ok=True)

        skill_file = target_dir / "SKILL.md"
        if skill_file.exists():
            raise ValueError(f"Skill '{name}' already exists at {skill_file}.")

        skill_file.write_text(content, encoding="utf-8")
        message = f"Created skill '{name}'."

    elif action == "edit":
        if not content:
            raise ValueError("content is required for 'edit'.")

        skill_dir = _find_skill_dir(name)
        if not skill_dir:
            raise ValueError(f"Skill '{name}' not found.")

        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        message = f"Edited skill '{name}'."

    elif action == "patch":
        skill_dir = _find_skill_dir(name)
        if not skill_dir:
            raise ValueError(f"Skill '{name}' not found.")

        target_file = skill_dir / file_path if file_path else skill_dir / "SKILL.md"
        if not target_file.is_file():
            raise FileNotFoundError(f"File not found: {target_file}")

        text = target_file.read_text(encoding="utf-8")

        if not replace_all:
            count = text.count(old_string)
            if count == 0:
                raise ValueError(f"'old_string' not found in {target_file}.")
            if count > 1:
                raise ValueError(f"'old_string' occurs {count}x in {target_file}; must be unique unless replace_all=True.")

        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        target_file.write_text(new_text, encoding="utf-8")
        message = f"Patched file in skill '{name}'."

    elif action == "delete":
        skill_dir = _find_skill_dir(name)
        if not skill_dir:
            raise ValueError(f"Skill '{name}' not found.")

        shutil.rmtree(skill_dir)
        message = f"Deleted skill '{name}'."
        if absorbed_into:
            message += f" (absorbed into '{absorbed_into}')."

    elif action == "write_file":
        skill_dir = _find_skill_dir(name)
        if not skill_dir:
            raise ValueError(f"Skill '{name}' not found.")
        if not file_path:
            raise ValueError("file_path is required for 'write_file'.")

        target_file = skill_dir / file_path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(file_content, encoding="utf-8")
        message = f"Wrote file '{file_path}' in skill '{name}'."

    elif action == "remove_file":
        skill_dir = _find_skill_dir(name)
        if not skill_dir:
            raise ValueError(f"Skill '{name}' not found.")
        if not file_path:
            raise ValueError("file_path is required for 'remove_file'.")

        target_file = skill_dir / file_path
        if target_file.is_file():
            target_file.unlink()
            message = f"Removed file '{file_path}' in skill '{name}'."
        else:
            raise FileNotFoundError(f"File '{file_path}' not found in skill '{name}'.")

    else:
        raise ValueError(f"Unknown action '{action}'.")

    task.add_log(agent, message)
    return ("skill_manage", message, {"action": action, "name": name})
