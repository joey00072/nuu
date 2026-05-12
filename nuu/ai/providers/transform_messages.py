"""
Cross-provider message transformation functions. Converts between nuu's
canonical Message types and provider-specific formats (OpenAI, Anthropic,
Google, etc.).

Owns: message conversion functions for known provider formats.
Delegates to: nuu.ai.types, image encoding utilities.

Depends on: nuu.ai.types, base64, hashlib
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..types import (
    AssistantMessage,
    ImageContent,
    Message,
    ModelInfo,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
)

NON_VISION_USER_IMAGE_PLACEHOLDER = "(image omitted: model does not support images)"
NON_VISION_TOOL_IMAGE_PLACEHOLDER = (
    "(tool image omitted: model does not support images)"
)


def replace_images_with_placeholder(
    content: list[TextContent | ImageContent],
    placeholder: str,
) -> list[TextContent]:
    result: list[TextContent] = []
    previous_was_placeholder = False

    for block in content:
        if block.type == "image":
            if not previous_was_placeholder:
                result.append(TextContent(type="text", text=placeholder))
            previous_was_placeholder = True
            continue

        result.append(block)
        previous_was_placeholder = block.text == placeholder

    return result


def downgrade_unsupported_images(
    messages: list[Message],
    model_info: ModelInfo,
) -> list[Message]:
    if "image" in model_info.input:
        return messages

    transformed: list[Message] = []
    for msg in messages:
        if msg.role == "user" and isinstance(msg.content, list):
            transformed.append(
                msg.model_copy(
                    update={
                        "content": replace_images_with_placeholder(
                            msg.content,
                            NON_VISION_USER_IMAGE_PLACEHOLDER,
                        ),
                    }
                )
            )
        elif msg.role == "toolResult":
            transformed.append(
                msg.model_copy(
                    update={
                        "content": replace_images_with_placeholder(
                            msg.content,
                            NON_VISION_TOOL_IMAGE_PLACEHOLDER,
                        ),
                    }
                )
            )
        else:
            transformed.append(msg)

    return transformed


def transform_messages(
    messages: list[Message],
    model_info: ModelInfo,
    normalize_tool_call_id: Callable[[str, ModelInfo, AssistantMessage], str]
    | None = None,
) -> list[Message]:
    tool_call_id_map: dict[str, str] = {}
    image_aware_messages = downgrade_unsupported_images(messages, model_info)

    transformed: list[Message] = []
    for msg in image_aware_messages:
        if msg.role == "user":
            transformed.append(msg)
            continue

        if msg.role == "toolResult":
            normalized_id = tool_call_id_map.get(msg.tool_call_id)
            if normalized_id is not None and normalized_id != msg.tool_call_id:
                transformed.append(
                    msg.model_copy(update={"tool_call_id": normalized_id})
                )
            else:
                transformed.append(msg)
            continue

        if msg.role == "assistant":
            assistant_msg: AssistantMessage = msg
            is_same_model = (
                assistant_msg.provider == model_info.provider
                and assistant_msg.api == model_info.api
                and assistant_msg.model == model_info.id
            )

            transformed_content: list[TextContent | ThinkingContent | ToolCall] = []
            for block in assistant_msg.content:
                if block.type == "thinking":
                    if block.redacted:
                        if is_same_model:
                            transformed_content.append(block)
                        continue
                    if is_same_model and block.thinking_signature:
                        transformed_content.append(block)
                        continue
                    if not block.thinking or block.thinking.strip() == "":
                        continue
                    if is_same_model:
                        transformed_content.append(block)
                    else:
                        transformed_content.append(
                            TextContent(type="text", text=block.thinking)
                        )

                elif block.type == "text":
                    if is_same_model:
                        transformed_content.append(block)
                    else:
                        transformed_content.append(
                            TextContent(type="text", text=block.text)
                        )

                elif block.type == "toolCall":
                    tool_call: ToolCall = block
                    if not is_same_model and tool_call.thought_signature:
                        tool_call = tool_call.model_copy(
                            update={"thought_signature": None}
                        )
                    if not is_same_model and normalize_tool_call_id is not None:
                        normalized_id = normalize_tool_call_id(
                            tool_call.id, model_info, assistant_msg
                        )
                        if normalized_id != tool_call.id:
                            tool_call_id_map[tool_call.id] = normalized_id
                            tool_call = tool_call.model_copy(
                                update={"id": normalized_id}
                            )
                    transformed_content.append(tool_call)

                else:
                    transformed_content.append(block)

            transformed.append(
                assistant_msg.model_copy(update={"content": transformed_content})
            )
            continue

        transformed.append(msg)

    result: list[Message] = []
    pending_tool_calls: list[ToolCall] = []
    existing_tool_result_ids: set[str] = set()

    def insert_synthetic_tool_results() -> None:
        nonlocal pending_tool_calls, existing_tool_result_ids
        if pending_tool_calls:
            for tc in pending_tool_calls:
                if tc.id not in existing_tool_result_ids:
                    result.append(
                        ToolResultMessage(
                            role="toolResult",
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            content=[
                                TextContent(type="text", text="No result provided")
                            ],
                            is_error=True,
                            timestamp=int(time.time() * 1000),
                        )
                    )
            pending_tool_calls = []
            existing_tool_result_ids = set()

    for msg in transformed:
        if msg.role == "assistant":
            insert_synthetic_tool_results()

            assistant_msg: AssistantMessage = msg
            if assistant_msg.stop_reason in ("error", "aborted"):
                continue

            tool_calls = [b for b in assistant_msg.content if b.type == "toolCall"]
            if tool_calls:
                pending_tool_calls = tool_calls
                existing_tool_result_ids = set()

            result.append(msg)

        elif msg.role == "toolResult":
            existing_tool_result_ids.add(msg.tool_call_id)
            result.append(msg)

        elif msg.role == "user":
            insert_synthetic_tool_results()
            result.append(msg)

        else:
            result.append(msg)

    insert_synthetic_tool_results()

    return result
