"""
Shared utilities for both Google AI and Google Vertex providers. Contains
message transformation logic common to both Google provider implementations.

Owns: Google-format message/tool conversion functions.
Delegates to: nuu.ai.types for message models.

Depends on: nuu.ai.types
"""

from __future__ import annotations

import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import Context, ModelInfo, Tool

_BASE64_SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_JSON_SCHEMA_META_DECLARATIONS = frozenset(
    {
        "$schema",
        "$id",
        "$anchor",
        "$dynamicAnchor",
        "$vocabulary",
        "$comment",
        "$defs",
        "definitions",
    }
)


def sanitize_surrogates(text: str) -> str:
    return re.sub(
        r"[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]",
        "",
        text,
    )


def is_thinking_part(part: dict[str, Any]) -> bool:
    return part.get("thought") is True


def retain_thought_signature(existing: str | None, incoming: str | None) -> str | None:
    if isinstance(incoming, str) and len(incoming) > 0:
        return incoming
    return existing


def _is_valid_thought_signature(signature: str | None) -> bool:
    if not signature:
        return False
    if len(signature) % 4 != 0:
        return False
    return bool(_BASE64_SIGNATURE_PATTERN.match(signature))


def _resolve_thought_signature(
    is_same_provider_and_model: bool, signature: str | None
) -> str | None:
    if is_same_provider_and_model and _is_valid_thought_signature(signature):
        return signature
    return None


def requires_tool_call_id(model_id: str) -> bool:
    return model_id.startswith("claude-") or model_id.startswith("gpt-oss-")


def _get_gemini_major_version(model_id: str) -> int | None:
    m = re.search(r"^gemini(?:-live)?-(\d+)", model_id.lower())
    return int(m.group(1)) if m else None


def _supports_multimodal_function_response(model_id: str) -> bool:
    v = _get_gemini_major_version(model_id)
    if v is not None:
        return v >= 3
    return True


def _sanitize_for_openapi(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    return {
        k: _sanitize_for_openapi(v)
        for k, v in schema.items()
        if k not in _JSON_SCHEMA_META_DECLARATIONS
    }


def convert_messages(model: ModelInfo, context: Context) -> list[dict[str, Any]]:
    from ..types import (
        AssistantMessage,
        ImageContent,
        TextContent,
        ThinkingContent,
        ToolCall,
        ToolResultMessage,
        UserMessage,
    )

    contents: list[dict[str, Any]] = []
    _NON_VISION_USER_IMAGE_PLACEHOLDER = (
        "(image omitted: model does not support images)"
    )
    _NON_VISION_TOOL_IMAGE_PLACEHOLDER = (
        "(tool image omitted: model does not support images)"
    )
    supports_images = "image" in model.input

    def _replace_images_with_placeholder(content, placeholder):
        result = []
        prev_was_placeholder = False
        for block in content:
            if isinstance(block, ImageContent):
                if not prev_was_placeholder:
                    result.append(TextContent(type="text", text=placeholder))
                prev_was_placeholder = True
            else:
                result.append(block)
                prev_was_placeholder = (
                    isinstance(block, TextContent) and block.text == placeholder
                )
        return result

    def _downgrade_images(messages):
        if supports_images:
            return messages
        converted = []
        for msg in messages:
            if isinstance(msg, UserMessage) and isinstance(msg.content, list):
                converted.append(
                    UserMessage(
                        role="user",
                        content=_replace_images_with_placeholder(
                            msg.content, _NON_VISION_USER_IMAGE_PLACEHOLDER
                        ),
                        timestamp=msg.timestamp,
                    )
                )
            elif isinstance(msg, ToolResultMessage):
                converted.append(
                    ToolResultMessage(
                        role="toolResult",
                        tool_call_id=msg.tool_call_id,
                        tool_name=msg.tool_name,
                        content=_replace_images_with_placeholder(
                            msg.content, _NON_VISION_TOOL_IMAGE_PLACEHOLDER
                        ),
                        is_error=msg.is_error,
                        timestamp=msg.timestamp,
                    )
                )
            else:
                converted.append(msg)
        return converted

    def _transform_messages(messages):
        tool_call_id_map = {}
        msgs = _downgrade_images(messages)

        transformed = []
        for msg in msgs:
            if msg.role == "user":
                transformed.append(msg)
            elif msg.role == "toolResult":
                normalized_id = tool_call_id_map.get(msg.tool_call_id)
                if normalized_id is not None and normalized_id != msg.tool_call_id:
                    transformed.append(
                        ToolResultMessage(
                            role="toolResult",
                            tool_call_id=normalized_id,
                            tool_name=msg.tool_name,
                            content=msg.content,
                            is_error=msg.is_error,
                            timestamp=msg.timestamp,
                        )
                    )
                else:
                    transformed.append(msg)
            elif msg.role == "assistant":
                is_same_model = msg.provider == model.provider and msg.model == model.id
                new_content = []
                for block in msg.content:
                    if isinstance(block, ThinkingContent):
                        if block.redacted:
                            if is_same_model:
                                new_content.append(block)
                            continue
                        if is_same_model and block.thinking_signature:
                            new_content.append(block)
                            continue
                        if not block.thinking or not block.thinking.strip():
                            continue
                        if is_same_model:
                            new_content.append(block)
                        else:
                            new_content.append(
                                TextContent(type="text", text=block.thinking)
                            )
                    elif isinstance(block, TextContent):
                        if is_same_model:
                            new_content.append(block)
                        else:
                            new_content.append(
                                TextContent(type="text", text=block.text)
                            )
                    elif isinstance(block, ToolCall):
                        tc = block
                        if not is_same_model and tc.thought_signature:
                            tc = ToolCall(
                                type="toolCall",
                                id=tc.id,
                                name=tc.name,
                                arguments=tc.arguments,
                                thought_signature=None,
                            )
                        if not is_same_model:
                            normalized_id = re.sub(r"[^a-zA-Z0-9_-]", "_", tc.id)[:64]
                            if normalized_id != tc.id:
                                tool_call_id_map[tc.id] = normalized_id
                                tc = ToolCall(
                                    type="toolCall",
                                    id=normalized_id,
                                    name=tc.name,
                                    arguments=tc.arguments,
                                    thought_signature=tc.thought_signature,
                                )
                        new_content.append(tc)
                    else:
                        new_content.append(block)

                transformed.append(
                    AssistantMessage(
                        role="assistant",
                        content=new_content,
                        api=msg.api,
                        provider=msg.provider,
                        model=msg.model,
                        usage=msg.usage,
                        stop_reason=msg.stop_reason,
                        timestamp=msg.timestamp,
                        error_message=msg.error_message,
                    )
                )
            else:
                transformed.append(msg)

        result = []
        pending_tool_calls: list[ToolCall] = []
        existing_tool_result_ids: set[str] = set()

        def _insert_synthetic():
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
                                timestamp=0,
                            )
                        )
                pending_tool_calls = []
                existing_tool_result_ids = set()

        for msg in transformed:
            if msg.role == "assistant":
                _insert_synthetic()
                if msg.stop_reason in ("error", "aborted"):
                    continue
                tool_calls = [b for b in msg.content if isinstance(b, ToolCall)]
                if tool_calls:
                    pending_tool_calls = tool_calls
                    existing_tool_result_ids = set()
                result.append(msg)
            elif msg.role == "toolResult":
                existing_tool_result_ids.add(msg.tool_call_id)
                result.append(msg)
            elif msg.role == "user":
                _insert_synthetic()
                result.append(msg)
            else:
                result.append(msg)

        _insert_synthetic()
        return result

    transformed = _transform_messages(context.messages)

    for msg in transformed:
        if msg.role == "user":
            if isinstance(msg.content, str):
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": sanitize_surrogates(msg.content)}],
                    }
                )
            else:
                parts = []
                for item in msg.content:
                    if isinstance(item, TextContent):
                        parts.append({"text": sanitize_surrogates(item.text)})
                    elif isinstance(item, ImageContent):
                        parts.append(
                            {
                                "inlineData": {
                                    "mimeType": item.mime_type,
                                    "data": item.data,
                                }
                            }
                        )
                if parts:
                    contents.append({"role": "user", "parts": parts})

        elif msg.role == "assistant":
            is_same = msg.provider == model.provider and msg.model == model.id
            parts = []
            for block in msg.content:
                if isinstance(block, TextContent):
                    if not block.text or not block.text.strip():
                        continue
                    sig = _resolve_thought_signature(is_same, block.text_signature)
                    part: dict[str, Any] = {"text": sanitize_surrogates(block.text)}
                    if sig:
                        part["thoughtSignature"] = sig
                    parts.append(part)
                elif isinstance(block, ThinkingContent):
                    if not block.thinking or not block.thinking.strip():
                        continue
                    if is_same:
                        sig = _resolve_thought_signature(
                            is_same, block.thinking_signature
                        )
                        part = {
                            "thought": True,
                            "text": sanitize_surrogates(block.thinking),
                        }
                        if sig:
                            part["thoughtSignature"] = sig
                        parts.append(part)
                    else:
                        parts.append({"text": sanitize_surrogates(block.thinking)})
                elif isinstance(block, ToolCall):
                    sig = _resolve_thought_signature(is_same, block.thought_signature)
                    fc: dict[str, Any] = {
                        "name": block.name,
                        "args": block.arguments or {},
                    }
                    if requires_tool_call_id(model.id):
                        fc["id"] = block.id
                    part = {"functionCall": fc}
                    if sig:
                        part["thoughtSignature"] = sig
                    parts.append(part)

            if parts:
                contents.append({"role": "model", "parts": parts})

        elif msg.role == "toolResult":
            text_content = [c for c in msg.content if isinstance(c, TextContent)]
            text_result = "\n".join(c.text for c in text_content)
            image_content = (
                [c for c in msg.content if isinstance(c, ImageContent)]
                if supports_images
                else []
            )

            has_text = len(text_result) > 0
            has_images = len(image_content) > 0
            supports_mmf = _supports_multimodal_function_response(model.id)

            response_value = (
                sanitize_surrogates(text_result)
                if has_text
                else ("(see attached image)" if has_images else "")
            )

            image_parts = [
                {"inlineData": {"mimeType": img.mime_type, "data": img.data}}
                for img in image_content
            ]

            include_id = requires_tool_call_id(model.id)
            func_resp: dict[str, Any] = {
                "functionResponse": {
                    "name": msg.tool_name,
                    "response": {"error": response_value}
                    if msg.is_error
                    else {"output": response_value},
                }
            }
            if has_images and supports_mmf:
                func_resp["functionResponse"]["parts"] = image_parts
            if include_id:
                func_resp["functionResponse"]["id"] = msg.tool_call_id

            if (
                contents
                and contents[-1].get("role") == "user"
                and any(
                    p.get("functionResponse") for p in contents[-1].get("parts", [])
                )
            ):
                contents[-1]["parts"].append(func_resp)
            else:
                contents.append({"role": "user", "parts": [func_resp]})

            if has_images and not supports_mmf:
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": "Tool result image:"}, *image_parts],
                    }
                )

    return contents


def convert_tools(
    tools: list[Tool],
    use_parameters: bool = False,
) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "functionDeclarations": [
                {
                    "name": t.name,
                    "description": t.description,
                    **(
                        {"parameters": _sanitize_for_openapi(t.parameters)}
                        if use_parameters
                        else {"parametersJsonSchema": t.parameters}
                    ),
                }
                for t in tools
            ]
        }
    ]


def map_tool_choice(choice: str) -> str:
    mapping = {
        "auto": "AUTO",
        "none": "NONE",
        "any": "ANY",
    }
    return mapping.get(choice, "AUTO")


def map_stop_reason(reason: str) -> str:
    mapping = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
    }
    return mapping.get(reason, "error")
