"""
Skill loading and formatting for the coding agent. Skills are markdown files
with YAML frontmatter providing specialized instructions for specific tasks.

Owns: Skill model, load_skill_from_file(), load_skills_from_dir(),
  format_skills_for_prompt().
Delegates to: nuu.coding_agent.utils.frontmatter for parsing.

Data flow: SKILL.md files -> parse_frontmatter() -> Skill models ->
  format_skills_for_prompt() -> system prompt inclusion

Depends on: nuu.coding_agent.utils.frontmatter, pydantic, os
"""

import os
from typing import List, Optional
from pydantic import BaseModel
from .utils.frontmatter import parse_frontmatter


class Skill(BaseModel):
    name: str
    description: str
    file_path: str
    disable_model_invocation: bool = False


def load_skill_from_file(file_path: str) -> Optional[Skill]:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        frontmatter, _ = parse_frontmatter(content)
        description = frontmatter.get("description")

        if not description:
            return None

        name = frontmatter.get("name") or os.path.basename(os.path.dirname(file_path))

        return Skill(
            name=name,
            description=description,
            file_path=file_path,
            disable_model_invocation=frontmatter.get("disable-model-invocation", False),
        )
    except Exception:
        return None


def load_skills_from_dir(directory: str) -> List[Skill]:
    skills = []
    if not os.path.exists(directory):
        return skills

    for root, dirs, files in os.walk(directory):
        if "node_modules" in dirs:
            dirs.remove("node_modules")
        if ".git" in dirs:
            dirs.remove(".git")

        for file in files:
            if file == "SKILL.md":
                skill = load_skill_from_file(os.path.join(root, file))
                if skill:
                    skills.append(skill)
                    # Don't recurse deeper if SKILL.md found?
                    # Pi does this to treat dir as skill root.
                    dirs.clear()
            elif file.endswith(".md") and root == directory:
                # Load top-level .md files as skills if they have frontmatter
                skill = load_skill_from_file(os.path.join(root, file))
                if skill:
                    skills.append(skill)

    return skills


def format_skills_for_prompt(skills: List[Skill]) -> str:
    visible_skills = [s for s in skills if not s.disable_model_invocation]
    if not visible_skills:
        return ""

    lines = [
        "\n\nThe following skills provide specialized instructions for specific tasks.",
        "Use the read tool to load a skill's file when the task matches its description.",
        "",
        "<available_skills>",
    ]

    for skill in visible_skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{skill.name}</name>")
        lines.append(f"    <description>{skill.description}</description>")
        lines.append(f"    <location>{skill.file_path}</location>")
        lines.append("  </skill>")

    lines.append("</available_skills>")
    return "\n".join(lines)
