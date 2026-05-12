"""
System prompt builder for the coding agent. Assembles the full system prompt
from a base template, tool descriptions, guidelines, project context files,
skills, and custom append sections.

Owns: build_system_prompt(), README/docs path helpers.
Delegates to: nuu.coding_agent.skills for skill formatting.

Data flow: configuration dicts + skills + context files -> build_system_prompt()
  -> assembled prompt string for LLM context

Depends on: nuu.coding_agent.config, nuu.coding_agent.skills
"""

from __future__ import annotations

from datetime import datetime

from nuu.coding_agent.config import get_agent_dir
from ..skills import format_skills_for_prompt, Skill


def get_readme_path() -> str:
    return str(get_agent_dir() / "README.md")


def get_docs_path() -> str:
    return str(get_agent_dir() / "docs")


def get_examples_path() -> str:
    return str(get_agent_dir() / "examples")


def build_system_prompt(
    custom_prompt: str | None = None,
    selected_tools: list[str] | None = None,
    tool_snippets: dict[str, str] | None = None,
    prompt_guidelines: list[str] | None = None,
    append_system_prompt: str | None = None,
    cwd: str = "",
    context_files: list[dict[str, str]] | None = None,
    skills: list[Skill] | None = None,
    agents_md_content: str | None = None,
    append_prompt: str | None = None,
) -> str:
    resolved_cwd = cwd.replace("\\", "/")

    now = datetime.now()
    date = f"{now.year}-{now.month:02d}-{now.day:02d}"

    append_section = f"\n\n{append_system_prompt}" if append_system_prompt else ""
    context_files = context_files or []
    skills = skills or []

    if custom_prompt:
        prompt = custom_prompt
        if append_section:
            prompt += append_section
        if append_prompt:
            prompt += f"\n\n{append_prompt}"
        if agents_md_content or context_files:
            prompt += "\n\n# Project Context\n\n"
            if agents_md_content:
                prompt += agents_md_content + "\n\n"
            for cf in context_files:
                prompt += f"## {cf['path']}\n\n{cf['content']}\n\n"
        has_read = not selected_tools or "read" in selected_tools
        if has_read and skills:
            prompt += format_skills_for_prompt(skills)
        prompt += f"\nCurrent date: {date}"
        prompt += f"\nCurrent working directory: {resolved_cwd}"
        return prompt

    readme_path = get_readme_path()
    docs_path = get_docs_path()
    examples_path = get_examples_path()

    tools = selected_tools or ["read", "bash", "edit", "write"]
    visible_tools = [t for t in tools if tool_snippets and t in tool_snippets]
    if visible_tools:
        tools_list = "\n".join(f"- {t}: {tool_snippets[t]}" for t in visible_tools)
    else:
        tools_list = "(none)"

    guidelines_list: list[str] = []
    guidelines_set: set[str] = set()

    def add_guideline(guideline: str) -> None:
        if guideline not in guidelines_set:
            guidelines_set.add(guideline)
            guidelines_list.append(guideline)

    has_bash = "bash" in tools
    has_grep = "grep" in tools
    has_find = "find" in tools
    has_ls = "ls" in tools

    if has_bash and not has_grep and not has_find and not has_ls:
        add_guideline("Use bash for file operations like ls, rg, find")
    elif has_bash and (has_grep or has_find or has_ls):
        add_guideline(
            "Prefer grep/find/ls tools over bash for file exploration "
            "(faster, respects .gitignore)"
        )

    for guideline in prompt_guidelines or []:
        normalized = guideline.strip()
        if normalized:
            add_guideline(normalized)

    add_guideline("Be concise in your responses")
    add_guideline("Show file paths clearly when working with files")

    guidelines = "\n".join(f"- {g}" for g in guidelines_list)

    prompt = (
        "You are an expert coding assistant operating inside pi, "
        "a coding agent harness. You help users by reading files, "
        "executing commands, editing code, and writing new files.\n\n"
        f"Available tools:\n{tools_list}\n\n"
        "In addition to the tools above, you may have access to "
        "other custom tools depending on the project.\n\n"
        f"Guidelines:\n{guidelines}\n\n"
        "Pi documentation (read only when the user asks about pi "
        "itself, its SDK, extensions, themes, skills, or TUI):\n"
        f"- Main documentation: {readme_path}\n"
        f"- Additional docs: {docs_path}\n"
        f"- Examples: {examples_path} "
        "(extensions, custom tools, SDK)\n"
        "- When asked about: extensions (docs/extensions.md, "
        "examples/extensions/), themes (docs/themes.md), skills "
        "(docs/skills.md), prompt templates "
        "(docs/prompt-templates.md), TUI components (docs/tui.md), "
        "keybindings (docs/keybindings.md), SDK integrations "
        "(docs/sdk.md), custom providers "
        "(docs/custom-provider.md), adding models "
        "(docs/models.md), pi packages (docs/packages.md)\n"
        "- When working on pi topics, read the docs and examples, "
        "and follow .md cross-references before implementing\n"
        "- Always read pi .md files completely and follow links "
        "to related docs (e.g., tui.md for TUI API details)"
    )

    if append_section:
        prompt += append_section

    if append_prompt:
        prompt += f"\n\n{append_prompt}"

    if agents_md_content or context_files:
        prompt += "\n\n# Project Context\n\n"
        if agents_md_content:
            prompt += agents_md_content + "\n\n"
        for cf in context_files:
            prompt += f"## {cf['path']}\n\n{cf['content']}\n\n"

    has_read = "read" in tools
    if has_read and skills:
        prompt += format_skills_for_prompt(skills)

    prompt += f"\nCurrent date: {date}"
    prompt += f"\nCurrent working directory: {resolved_cwd}"

    return prompt
