"""
Session compaction: detects when context exceeds model window, generates
summaries via an LLM call, and compresses the conversation history. Tracks
file operations across the session for context preservation.

Owns: should_compact(), generate_summary(), serialize_conversation(),
  FileOperations tracker, CompactionSettings/Result.
Delegates to: nuu.ai.stream.complete_simple for summarization,
  nuu.agent.types for AgentMessage/ThinkingLevel.

Data flow: messages + model -> estimate_tokens() -> should_compact() ->
  generate_summary() -> compaction summary string

Depends on: nuu.agent.types, nuu.ai.types, nuu.ai.stream
"""

import json
import math
import time
from typing import Any, List, Optional, Set
from pydantic import BaseModel

from ..agent.types import AgentMessage, ThinkingLevel
from ..ai.types import Message, ModelInfo, Usage, TextContent
from ..ai.stream import complete_simple

# ============================================================================
# Types & Settings
# ============================================================================


class CompactionSettings(BaseModel):
    enabled: bool = True
    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000


DEFAULT_COMPACTION_SETTINGS = CompactionSettings()


class CompactionResult(BaseModel):
    summary: str
    first_kept_entry_id: str
    tokens_before: int
    details: Optional[Any] = None


# ============================================================================
# Token Calculation
# ============================================================================


def estimate_tokens(message: AgentMessage) -> int:
    chars = 0
    role = getattr(message, "role", None)

    if role == "user":
        content = message.content
        if isinstance(content, str):
            chars = len(content)
        elif isinstance(content, list):
            for block in content:
                if block.type == "text":
                    chars += len(block.text)
    elif role == "assistant":
        for block in message.content:
            if block.type == "text":
                chars += len(block.text)
            elif block.type == "thinking":
                chars += len(block.thinking)
            elif block.type == "toolCall":
                chars += len(block.name) + len(json.dumps(block.arguments))
    elif role in ("toolResult", "custom"):
        if isinstance(message.content, str):
            chars = len(message.content)
        else:
            for block in message.content:
                if block.type == "text":
                    chars += len(block.text)
                elif block.type == "image":
                    chars += 4800  # Estimate

    return math.ceil(chars / 4)


def calculate_context_tokens(usage: Usage) -> int:
    return usage.total_tokens or (
        usage.input + usage.output + usage.cache_read + usage.cache_write
    )


def should_compact(
    context_tokens: int, context_window: int, settings: CompactionSettings
) -> bool:
    if not settings.enabled:
        return False
    return context_tokens > (context_window - settings.reserve_tokens)


# ============================================================================
# File Operation Tracking
# ============================================================================


class FileOperations:
    def __init__(self):
        self.read: Set[str] = set()
        self.written: Set[str] = set()
        self.edited: Set[str] = set()


def extract_file_ops_from_message(message: AgentMessage, file_ops: FileOperations):
    if getattr(message, "role", None) != "assistant":
        return

    for block in getattr(message, "content", []):
        if block.type == "toolCall":
            path = block.arguments.get("path")
            if not isinstance(path, str):
                continue

            if block.name == "read":
                file_ops.read.add(path)
            elif block.name == "write":
                file_ops.written.add(path)
            elif block.name == "edit":
                file_ops.edited.add(path)


def format_file_operations(file_ops: FileOperations) -> str:
    modified = file_ops.edited | file_ops.written
    read_only = sorted([f for f in file_ops.read if f not in modified])
    modified_files = sorted(list(modified))

    sections = []
    if read_only:
        sections.append(f"<read-files>\n{chr(10).join(read_only)}\n</read-files>")
    if modified_files:
        sections.append(
            f"<modified-files>\n{chr(10).join(modified_files)}\n</modified-files>"
        )

    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


# ============================================================================
# Serialization for Summary
# ============================================================================


def serialize_conversation(messages: List[Message]) -> str:
    parts = []
    for msg in messages:
        role = msg.role
        if role == "user":
            content = msg.content
            if isinstance(content, list):
                text = "".join([b.text for b in content if b.type == "text"])
            else:
                text = content
            parts.append(f"[User]: {text}")
        elif role == "assistant":
            text_parts = []
            thinking_parts = []
            tool_calls = []
            for block in msg.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "thinking":
                    thinking_parts.append(block.thinking)
                elif block.type == "toolCall":
                    args_str = ", ".join(
                        [f"{k}={json.dumps(v)}" for k, v in block.arguments.items()]
                    )
                    tool_calls.append(f"{block.name}({args_str})")

            if thinking_parts:
                parts.append(f"[Assistant thinking]: {chr(10).join(thinking_parts)}")
            if text_parts:
                parts.append(f"[Assistant]: {chr(10).join(text_parts)}")
            if tool_calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(tool_calls)}")
        elif role == "toolResult":
            text = "".join([b.text for b in msg.content if b.type == "text"])
            # Truncate long tool results
            if len(text) > 2000:
                text = text[:2000] + "\n\n[... truncated]"
            parts.append(f"[Tool result]: {text}")

    return "\n\n".join(parts)


# ============================================================================
# Summarization Prompt
# ============================================================================

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a conversation "
    "between a user and an AI coding assistant, then produce a structured summary. "
    "Do NOT continue the conversation. ONLY output the structured summary."
)

SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish?]

## Constraints & Preferences
- [Any constraints, preferences, or requirements]

## Progress
### Done
- [x] [Completed tasks]

### In Progress
- [ ] [Current work]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

# ============================================================================
# Main Compaction Functions
# ============================================================================


async def generate_summary(
    messages: List[AgentMessage],
    model: ModelInfo,
    api_key: str,
    thinking_level: ThinkingLevel = "off",
) -> str:
    conversation_text = serialize_conversation(messages)
    prompt_text = f"<conversation>\n{conversation_text}\n</conversation>\n\n{SUMMARIZATION_PROMPT}"

    from ..ai.types import UserMessage, Context

    summarization_messages = [
        UserMessage(
            role="user",
            content=[TextContent(type="text", text=prompt_text)],
            timestamp=int(time.time() * 1000),
        )
    ]

    ai_context = Context(
        system_prompt=SUMMARIZATION_SYSTEM_PROMPT, messages=summarization_messages
    )

    from ..ai.types import SimpleStreamOptions

    options = SimpleStreamOptions(api_key=api_key)
    response = await complete_simple(model, ai_context, options=options)

    if response.stop_reason == "error":
        raise RuntimeError(f"Summarization failed: {response.error_message}")

    text_content = "".join([b.text for b in response.content if b.type == "text"])
    return text_content
