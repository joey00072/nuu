"""
Prompt template discovery and management. Loads prompt templates from the
agent's prompts directory, parses YAML frontmatter, and provides template
rendering for dynamic system prompt generation.

Owns: prompt template loading, frontmatter parsing, template rendering.
Delegates to: nuu.coding_agent.utils.frontmatter for YAML parsing.

Data flow: .md files -> parse_frontmatter() -> PromptTemplate -> render()

Depends on: nuu.coding_agent.utils.frontmatter, pathlib
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_PROMPTS_DIR = Path.home() / ".nuu" / "prompts"


@dataclass
class PromptTemplate:
    name: str
    description: str
    argument_hint: str = ""
    template_text: str = ""
    source_info: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""


def parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    frontmatter: dict[str, str] = {}
    for line in content[4:end].strip().split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            frontmatter[key.strip()] = value.strip().strip('"').strip("'")
    body = content[end + 4 :].strip()
    return frontmatter, body


def load_prompt_templates(prompts_dir: Path) -> list[PromptTemplate]:
    templates: list[PromptTemplate] = []
    if not prompts_dir.is_dir():
        return templates
    for entry in sorted(prompts_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".md":
            continue
        try:
            raw = entry.read_text("utf-8")
        except Exception:
            continue
        frontmatter, body = parse_frontmatter(raw)
        name = entry.stem
        templates.append(
            PromptTemplate(
                name=name,
                description=frontmatter.get("description", name),
                argument_hint=frontmatter.get("argument-hint", ""),
                template_text=body,
                file_path=str(entry),
            )
        )
    return templates


def format_prompt(template: PromptTemplate, args: dict[str, str]) -> str:
    result = template.template_text
    for key, value in args.items():
        result = result.replace(f"{{{key}}}", value)
    return result


_BUILTIN_TEMPLATES: dict[str, PromptTemplate] = {}


def get_builtin_prompt(name: str) -> PromptTemplate | None:
    return _BUILTIN_TEMPLATES.get(name)
