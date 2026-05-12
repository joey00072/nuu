"""
Message model extensions for the coding agent. Defines additional message
types and utilities beyond the base nuu.ai.types message set.

Owns: extended message models for coding-agent-specific use cases.
Delegates to: nuu.ai.types for base message types, pydantic for validation.

Depends on: nuu.ai.types, pydantic
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ...ai.types import (
    ImageContent,
    TextContent,
    UserMessage,
    Message as LlmMessage,
)

COMPACTION_SUMMARY_PREFIX = (
    "The conversation history before this point was compacted "
    "into the following summary:\n\n<summary>\n"
)

COMPACTION_SUMMARY_SUFFIX = "\n</summary>"

BRANCH_SUMMARY_PREFIX = (
    "The following is a summary of a branch that this "
    "conversation came back from:\n\n<summary>\n"
)

BRANCH_SUMMARY_SUFFIX = "</summary>"


class BashExecutionMessage(BaseModel):
    role: str = "bashExecution"
    command: str
    output: str
    exit_code: int | None = None
    cancelled: bool = False
    truncated: bool = False
    full_output_path: str | None = None
    timestamp: int = 0
    exclude_from_context: bool = False


class CustomMessage(BaseModel):
    role: str = "custom"
    custom_type: str
    content: str | list[TextContent | ImageContent]
    display: bool = True
    details: Any = None
    timestamp: int = 0


class BranchSummaryMessage(BaseModel):
    role: str = "branchSummary"
    summary: str
    from_id: str
    timestamp: int = 0


class CompactionSummaryMessage(BaseModel):
    role: str = "compactionSummary"
    summary: str
    tokens_before: int = 0
    timestamp: int = 0


def bash_execution_to_text(msg: BashExecutionMessage) -> str:
    text = f"Ran `{msg.command}`\n"
    if msg.output:
        text += f"```\n{msg.output}\n```"
    else:
        text += "(no output)"
    if msg.cancelled:
        text += "\n\n(command cancelled)"
    elif msg.exit_code is not None and msg.exit_code != 0:
        text += f"\n\nCommand exited with code {msg.exit_code}"
    if msg.truncated and msg.full_output_path:
        text += f"\n\n[Output truncated. Full output: {msg.full_output_path}]"
    return text


def convert_to_llm(messages: list) -> list[LlmMessage]:
    result: list[LlmMessage] = []
    for m in messages:
        role = getattr(m, "role", None)
        if role == "bashExecution":
            if getattr(m, "exclude_from_context", False):
                continue
            result.append(
                UserMessage(
                    role="user",
                    content=[TextContent(type="text", text=bash_execution_to_text(m))],
                    timestamp=getattr(m, "timestamp", 0),
                )
            )
        elif role == "custom":
            content = (
                [TextContent(type="text", text=m.content)]
                if isinstance(m.content, str)
                else m.content
            )
            result.append(
                UserMessage(
                    role="user",
                    content=content,
                    timestamp=getattr(m, "timestamp", 0),
                )
            )
        elif role == "branchSummary":
            result.append(
                UserMessage(
                    role="user",
                    content=[
                        TextContent(
                            type="text",
                            text=BRANCH_SUMMARY_PREFIX
                            + m.summary
                            + BRANCH_SUMMARY_SUFFIX,
                        )
                    ],
                    timestamp=getattr(m, "timestamp", 0),
                )
            )
        elif role == "compactionSummary":
            result.append(
                UserMessage(
                    role="user",
                    content=[
                        TextContent(
                            type="text",
                            text=COMPACTION_SUMMARY_PREFIX
                            + m.summary
                            + COMPACTION_SUMMARY_SUFFIX,
                        )
                    ],
                    timestamp=getattr(m, "timestamp", 0),
                )
            )
        elif role in ("user", "assistant", "toolResult"):
            result.append(m)
    return result
