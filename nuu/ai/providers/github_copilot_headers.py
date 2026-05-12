"""
GitHub Copilot-specific header and message transformation utilities. Handles
the special auth header format and message schema expected by Copilot's API.

Owns: Copilot header construction, message conversion.
Delegates to: nuu.ai.types for message models.

Depends on: nuu.ai.types
"""

from __future__ import annotations

from typing import Literal

from ..types import ImageContent, Message, UserMessage, ToolResultMessage


def infer_copilot_initiator(messages: list[Message]) -> Literal["user", "agent"]:
    last = messages[-1] if messages else None
    return "agent" if last and last.role != "user" else "user"


def has_copilot_vision_input(messages: list[Message]) -> bool:
    for msg in messages:
        if isinstance(msg, UserMessage) and isinstance(msg.content, list):
            if any(isinstance(c, ImageContent) for c in msg.content):
                return True
        if isinstance(msg, ToolResultMessage):
            if any(isinstance(c, ImageContent) for c in msg.content):
                return True
    return False


def build_copilot_dynamic_headers(
    messages: list[Message], has_images: bool
) -> dict[str, str]:
    headers: dict[str, str] = {
        "X-Initiator": infer_copilot_initiator(messages),
        "Openai-Intent": "conversation-edits",
    }

    if has_images:
        headers["Copilot-Vision-Request"] = "true"

    return headers
