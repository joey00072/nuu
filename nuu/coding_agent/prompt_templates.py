"""
Prompt template loading and management. Loads .md files from the agent's
prompt-templates directory, parses YAML frontmatter, and provides template
rendering for dynamic system prompt generation.

Owns: PromptTemplate model, template file discovery and parsing.
Delegates to: nuu.coding_agent.utils.frontmatter for YAML parsing, os/pathlib
  for file I/O.

Depends on: nuu.coding_agent.utils.frontmatter, pydantic, pathlib
"""

import os
from typing import List, Optional
from pydantic import BaseModel
from .utils.frontmatter import parse_frontmatter


class PromptTemplate(BaseModel):
    name: str
    description: str
    content: str
    file_path: str


def parse_command_args(args_string: str) -> List[str]:
    """
    Parse command arguments respecting quoted strings.
    """
    args = []
    current = ""
    in_quote = None

    for char in args_string:
        if in_quote:
            if char == in_quote:
                in_quote = None
            else:
                current += char
        elif char in ('"', "'"):
            in_quote = char
        elif char.isspace():
            if current:
                args.append(current)
                current = ""
        else:
            current += char

    if current:
        args.append(current)
    return args


def substitute_args(content: str, args: List[str]) -> str:
    result = content

    # Replace $1, $2, etc.
    for i, arg in enumerate(args, 1):
        result = result.replace(f"${i}", arg)

    # Replace $@ and $ARGUMENTS
    all_args = " ".join(args)
    result = result.replace("$@", all_args)
    result = result.replace("$ARGUMENTS", all_args)

    return result


def load_template_from_file(file_path: str) -> Optional[PromptTemplate]:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        frontmatter, body = parse_frontmatter(content)
        name = os.path.basename(file_path).replace(".md", "")
        description = frontmatter.get(
            "description", body[:60] + "..." if len(body) > 60 else body
        )

        return PromptTemplate(
            name=name, description=description, content=body, file_path=file_path
        )
    except Exception:
        return None


def load_prompt_templates_from_dir(directory: str) -> List[PromptTemplate]:
    templates = []
    if not os.path.exists(directory):
        return templates

    for file in os.listdir(directory):
        if file.endswith(".md"):
            template = load_template_from_file(os.path.join(directory, file))
            if template:
                templates.append(template)
    return templates


def expand_prompt_template(text: str, templates: List[PromptTemplate]) -> str:
    if not text.startswith("/"):
        return text

    parts = text.split(" ", 1)
    template_name = parts[0][1:]
    args_string = parts[1] if len(parts) > 1 else ""

    template = next((t for t in templates if t.name == template_name), None)
    if template:
        args = parse_command_args(args_string)
        return substitute_args(template.content, args)

    return text
