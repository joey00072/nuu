"""
Compaction utility types and shared logic: token estimation, file operation
tracking, conversation serialization, and summarization prompt templates.

Owns: CompactionSettings, FileOperations, serialize_conversation(),
  estimate_tokens(), format_file_operations().
Delegates to: json for serialization, math for token estimation.

Depends on: standard library only (json, math, typing)
"""

from __future__ import annotations

import json
import math
from typing import Any

from ...ai.types import Message

TOOL_RESULT_MAX_CHARS = 2000


class FileOperations:
    def __init__(self) -> None:
        self.read: set[str] = set()
        self.written: set[str] = set()
        self.edited: set[str] = set()


def create_file_ops() -> FileOperations:
    return FileOperations()


def extract_file_ops_from_message(message: Any, file_ops: FileOperations) -> None:
    if getattr(message, "role", None) != "assistant":
        return
    blocks = getattr(message, "content", None)
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if getattr(block, "type", None) != "toolCall":
            continue
        name = getattr(block, "name", None)
        arguments = getattr(block, "arguments", None)
        if not arguments or not isinstance(arguments, dict):
            continue
        path = arguments.get("path")
        if not isinstance(path, str):
            continue
        if name == "read":
            file_ops.read.add(path)
        elif name == "write":
            file_ops.written.add(path)
        elif name == "edit":
            file_ops.edited.add(path)


def compute_file_lists(file_ops: FileOperations) -> tuple[list[str], list[str]]:
    modified = file_ops.edited | file_ops.written
    read_files = sorted(f for f in file_ops.read if f not in modified)
    modified_files = sorted(modified)
    return read_files, modified_files


def format_file_operations(read_files: list[str], modified_files: list[str]) -> str:
    sections: list[str] = []
    if read_files:
        sections.append("<read-files>\n" + "\n".join(read_files) + "\n</read-files>")
    if modified_files:
        sections.append(
            "<modified-files>\n" + "\n".join(modified_files) + "\n</modified-files>"
        )
    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


def truncate_for_summary(text: str, max_chars: int = TOOL_RESULT_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    truncated_chars = len(text) - max_chars
    return f"{text[:max_chars]}\n\n[... {truncated_chars} more characters truncated]"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def _estimate_message_tokens(msg: Message) -> int:
    role = msg.role
    chars = 0
    if role == "user":
        content = msg.content
        if isinstance(content, str):
            chars = len(content)
        elif isinstance(content, list):
            for block in content:
                if block.type == "text":
                    chars += len(block.text)
    elif role == "assistant":
        for block in msg.content:
            if block.type == "text":
                chars += len(block.text)
            elif block.type == "thinking":
                chars += len(block.thinking)
            elif block.type == "toolCall":
                chars += len(block.name) + len(json.dumps(block.arguments))
    elif role == "toolResult":
        for block in msg.content:
            if block.type == "text":
                chars += len(block.text)
            elif block.type == "image":
                chars += 4800
    return math.ceil(chars / 4)


def calculate_context_tokens(messages: list[Message]) -> int:
    total = 0
    for msg in messages:
        total += _estimate_message_tokens(msg)
    return total


def should_compact(
    messages: list[Message], context_window: int, reserve_tokens: int = 16384
) -> bool:
    total = calculate_context_tokens(messages)
    return total > context_window - reserve_tokens


def find_cut_point(messages: list[Message], max_tokens: int) -> int:
    if not messages:
        return 0
    kept_tokens = _estimate_message_tokens(messages[0])
    cut_point = len(messages)
    for i in range(len(messages) - 1, 0, -1):
        tokens = _estimate_message_tokens(messages[i])
        if kept_tokens + tokens > max_tokens:
            break
        kept_tokens += tokens
        cut_point = i
    return cut_point


def serialize_conversation(messages: list[Message]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = msg.role
        if role == "user":
            content = (
                msg.content
                if isinstance(msg.content, str)
                else "".join(b.text for b in msg.content if b.type == "text")
            )
            if content:
                parts.append(f"[User]: {content}")
        elif role == "assistant":
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls: list[str] = []
            for block in msg.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "thinking":
                    thinking_parts.append(block.thinking)
                elif block.type == "toolCall":
                    args_str = ", ".join(
                        f"{k}={json.dumps(v)}" for k, v in block.arguments.items()
                    )
                    tool_calls.append(f"{block.name}({args_str})")
            if thinking_parts:
                parts.append("[Assistant thinking]: " + "\n".join(thinking_parts))
            if text_parts:
                parts.append("[Assistant]: " + "\n".join(text_parts))
            if tool_calls:
                parts.append("[Assistant tool calls]: " + "; ".join(tool_calls))
        elif role == "toolResult":
            text = "".join(b.text for b in msg.content if b.type == "text")
            if text:
                parts.append(f"[Tool result]: {truncate_for_summary(text)}")
    return "\n\n".join(parts)


class CompactionSettings:
    def __init__(
        self,
        enabled: bool = True,
        reserve_tokens: int = 16384,
        keep_recent_tokens: int = 20000,
    ) -> None:
        self.enabled = enabled
        self.reserve_tokens = reserve_tokens
        self.keep_recent_tokens = keep_recent_tokens


DEFAULT_COMPACTION_SETTINGS = CompactionSettings()


SUMMARIZATION_SYSTEM_PROMPT = """You are a context summarization assistant. Your task is to read a conversation between a user and an AI coding assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary."""
