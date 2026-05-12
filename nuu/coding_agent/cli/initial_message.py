"""
Builds the initial user message for a coding agent session. Combines the
prompt text, stdin content, and file specifications into a single message.

Owns: build_initial_message().
Delegates to: nuu.coding_agent.cli.args (ParsedArgs).

Data flow: ParsedArgs + stdin_content -> build_initial_message() -> message dict

Depends on: nuu.coding_agent.cli.args
"""

from __future__ import annotations

from typing import TypedDict

from .args import ParsedArgs


class InitialMessageInput(TypedDict, total=False):
    parsed: ParsedArgs
    file_text: str
    stdin_content: str


class InitialMessageResult(TypedDict, total=False):
    initial_message: str


def build_initial_message(input_data: InitialMessageInput) -> InitialMessageResult:
    parsed = input_data.get("parsed", {})
    file_text = input_data.get("file_text", "")
    stdin_content = input_data.get("stdin_content")

    parts: list[str] = []

    if stdin_content:
        parts.append(stdin_content)

    if file_text:
        parts.append(file_text)

    prompt = parsed.get("prompt")
    if prompt:
        parts.append(prompt)

    if parts:
        return {"initial_message": "".join(parts)}

    return {}
