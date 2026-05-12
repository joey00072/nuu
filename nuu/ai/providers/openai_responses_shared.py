"""
Shared utilities shared across OpenAI Responses, OpenAI Codex Responses, and
Azure OpenAI Responses providers. Contains message transformation, tool
formatting, response parsing, and SSE event processing logic.

Owns: OpenAI-format message/tool conversion, SSE event dispatch.
Delegates to: nuu.ai.types for message models.

Data flow: raw SSE events -> standardized AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.utils.json_parse
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterable
from typing import Any, TypedDict

from ..event_stream import AssistantMessageEventStream
from ..models import calculate_cost
from ..types import (
    AssistantMessage,
    Context,
    ImageContent,
    Message,
    ModelInfo,
    StopReason,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

# =============================================================================
# Utilities
# =============================================================================

NON_VISION_USER_IMAGE_PLACEHOLDER = "(image omitted: model does not support images)"
NON_VISION_TOOL_IMAGE_PLACEHOLDER = (
    "(tool image omitted: model does not support images)"
)


def encode_text_signature_v1(id: str, phase: str | None = None) -> str:
    payload: dict[str, Any] = {"v": 1, "id": id}
    if phase:
        payload["phase"] = phase
    return json.dumps(payload)


def parse_text_signature(signature: str | None) -> dict[str, Any] | None:
    if not signature:
        return None
    if signature.startswith("{"):
        try:
            parsed = json.loads(signature)
            if parsed.get("v") == 1 and isinstance(parsed.get("id"), str):
                result: dict[str, Any] = {"id": parsed["id"]}
                phase = parsed.get("phase")
                if phase in ("commentary", "final_answer"):
                    result["phase"] = phase
                return result
        except json.JSONDecodeError:
            pass
    return {"id": signature}


def short_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:8]


def parse_streaming_json(partial: str) -> dict[str, Any]:
    if not partial:
        return {}
    try:
        return json.loads(partial)
    except json.JSONDecodeError:
        pass
    depth = 0
    in_string = False
    escape = False
    start = 0
    for i, ch in enumerate(partial):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            if depth == 0:
                start = i
            depth += 1
        elif ch in ("}", "]"):
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(partial[start : i + 1])
                except json.JSONDecodeError:
                    pass
    return {}


def sanitize_surrogates(text: str) -> str:
    return re.sub(r"[\ud800-\udfff]", "", text)


# =============================================================================
# Options types
# =============================================================================


class OpenAIResponsesStreamOptions(TypedDict, total=False):
    service_tier: str | None
    resolve_service_tier: Any
    apply_service_tier_pricing: Any


class ConvertResponsesMessagesOptions(TypedDict, total=False):
    include_system_prompt: bool


class ConvertResponsesToolsOptions(TypedDict, total=False):
    strict: bool | None


# =============================================================================
# Constants
# =============================================================================

OPENAI_TOOL_CALL_PROVIDERS = frozenset(
    {
        "openai",
        "openai-codex",
        "opencode",
    }
)


# =============================================================================
# Message transformation helpers
# =============================================================================


def _replace_images_with_placeholder(
    content: list[TextContent | ImageContent],
    placeholder: str,
) -> list[TextContent]:
    result: list[TextContent] = []
    previous_was_placeholder = False
    for block in content:
        if isinstance(block, ImageContent) or block.type == "image":
            if not previous_was_placeholder:
                result.append(TextContent(type="text", text=placeholder))
            previous_was_placeholder = True
            continue
        result.append(block)
        previous_was_placeholder = block.text == placeholder
    return result


def _downgrade_unsupported_images(
    messages: list[Message],
    model: ModelInfo,
) -> list[Message]:
    if model.input and "image" in model.input:
        return messages
    result: list[Message] = []
    for msg in messages:
        if msg.role == "user" and isinstance(msg.content, list):
            new_content = _replace_images_with_placeholder(
                msg.content, NON_VISION_USER_IMAGE_PLACEHOLDER
            )
            result.append(
                UserMessage(
                    role="user",
                    content=new_content,
                    timestamp=msg.timestamp,
                )
            )
        elif msg.role == "toolResult":
            new_content = _replace_images_with_placeholder(
                msg.content, NON_VISION_TOOL_IMAGE_PLACEHOLDER
            )
            result.append(
                ToolResultMessage(
                    role="toolResult",
                    tool_call_id=msg.tool_call_id,
                    tool_name=msg.tool_name,
                    content=new_content,
                    details=msg.details,
                    is_error=msg.is_error,
                    timestamp=msg.timestamp,
                )
            )
        else:
            result.append(msg)
    return result


def transform_messages(
    messages: list[Message],
    model: ModelInfo,
    normalize_tool_call_id: Any = None,
) -> list[Message]:
    tool_call_id_map: dict[str, str] = {}
    image_aware_messages = _downgrade_unsupported_images(messages, model)

    transformed: list[Message] = []
    for msg in image_aware_messages:
        if msg.role == "user":
            transformed.append(msg)
        elif msg.role == "toolResult":
            normalized_id = tool_call_id_map.get(msg.tool_call_id)
            if normalized_id and normalized_id != msg.tool_call_id:
                transformed.append(
                    ToolResultMessage(
                        role="toolResult",
                        tool_call_id=normalized_id,
                        tool_name=msg.tool_name,
                        content=msg.content,
                        details=msg.details,
                        is_error=msg.is_error,
                        timestamp=msg.timestamp,
                    )
                )
            else:
                transformed.append(msg)
        elif msg.role == "assistant":
            assistant_msg = msg
            is_same_model = (
                assistant_msg.provider == model.provider
                and assistant_msg.api == model.api
                and assistant_msg.model == model.id
            )

            new_content: list[TextContent | ThinkingContent | ToolCall] = []
            for block in assistant_msg.content:
                if block.type == "thinking":
                    if getattr(block, "redacted", False):
                        if is_same_model:
                            new_content.append(block)
                        continue
                    if is_same_model and block.thinking_signature:
                        new_content.append(block)
                    elif not block.thinking or block.thinking.strip() == "":
                        continue
                    elif is_same_model:
                        new_content.append(block)
                    else:
                        new_content.append(
                            TextContent(
                                type="text",
                                text=block.thinking,
                            )
                        )
                elif block.type == "text":
                    if is_same_model:
                        new_content.append(block)
                    else:
                        new_content.append(
                            TextContent(
                                type="text",
                                text=block.text,
                            )
                        )
                elif block.type == "toolCall":
                    tc = block
                    normalized_tc: ToolCall = tc
                    if not is_same_model and tc.thought_signature:
                        tc_dict = tc.model_dump()
                        tc_dict.pop("thought_signature", None)
                        normalized_tc = ToolCall(**tc_dict)

                    if not is_same_model and normalize_tool_call_id:
                        normalized_id = normalize_tool_call_id(
                            tc.id, model, assistant_msg
                        )
                        if normalized_id != tc.id:
                            tool_call_id_map[tc.id] = normalized_id
                            tc_dict = normalized_tc.model_dump()
                            tc_dict["id"] = normalized_id
                            normalized_tc = ToolCall(**tc_dict)

                    new_content.append(normalized_tc)

            if assistant_msg.stop_reason in ("error", "aborted"):
                continue

            transformed.append(
                AssistantMessage(
                    role="assistant",
                    content=new_content,
                    api=assistant_msg.api,
                    provider=assistant_msg.provider,
                    model=assistant_msg.model,
                    response_model=assistant_msg.response_model,
                    response_id=assistant_msg.response_id,
                    usage=assistant_msg.usage,
                    stop_reason=assistant_msg.stop_reason,
                    error_message=assistant_msg.error_message,
                    timestamp=assistant_msg.timestamp,
                )
            )
        else:
            transformed.append(msg)

    result: list[Message] = []
    pending_tool_calls: list[ToolCall] = []
    existing_tool_result_ids: set[str] = set()

    def insert_synthetic_tool_results():
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
            insert_synthetic_tool_results()
            assistant_msg = msg
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


# =============================================================================
# Message conversion
# =============================================================================


def convert_responses_messages(
    model: ModelInfo,
    context: Context,
    allowed_tool_call_providers: frozenset[str] | None = None,
    options: ConvertResponsesMessagesOptions | None = None,
) -> list[dict[str, Any]]:
    if allowed_tool_call_providers is None:
        allowed_tool_call_providers = OPENAI_TOOL_CALL_PROVIDERS

    messages: list[dict[str, Any]] = []

    def _normalize_id_part(part: str) -> str:
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", part)
        normalized = sanitized[:64] if len(sanitized) > 64 else sanitized
        return normalized.rstrip("_")

    def _build_foreign_responses_item_id(item_id: str) -> str:
        normalized = f"fc_{short_hash(item_id)}"
        return normalized[:64] if len(normalized) > 64 else normalized

    def _normalize_tool_call_id(
        id: str, target_model: ModelInfo, source: AssistantMessage
    ) -> str:
        if model.provider not in allowed_tool_call_providers:
            return _normalize_id_part(id)
        if "|" not in id:
            return _normalize_id_part(id)
        call_id, item_id = id.split("|", 1)
        normalized_call_id = _normalize_id_part(call_id)
        is_foreign_tool_call = (
            source.provider != model.provider or source.api != model.api
        )
        if is_foreign_tool_call:
            normalized_item_id = _build_foreign_responses_item_id(item_id)
        else:
            normalized_item_id = _normalize_id_part(item_id)
        if not normalized_item_id.startswith("fc_"):
            normalized_item_id = _normalize_id_part(f"fc_{normalized_item_id}")
        return f"{normalized_call_id}|{normalized_item_id}"

    transformed = transform_messages(context.messages, model, _normalize_tool_call_id)

    include_system_prompt = True
    if options:
        include_system_prompt = options.get("include_system_prompt", True)
    if include_system_prompt and context.system_prompt:
        role = "developer" if model.reasoning else "system"
        messages.append(
            {
                "role": role,
                "content": sanitize_surrogates(context.system_prompt),
            }
        )

    msg_index = 0
    for msg in transformed:
        if msg.role == "user":
            if isinstance(msg.content, str):
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": sanitize_surrogates(msg.content),
                            }
                        ],
                    }
                )
            else:
                content: list[dict[str, Any]] = []
                for item in msg.content:
                    if item.type == "text":
                        content.append(
                            {
                                "type": "input_text",
                                "text": sanitize_surrogates(item.text),
                            }
                        )
                    elif item.type == "image":
                        content.append(
                            {
                                "type": "input_image",
                                "detail": "auto",
                                "image_url": f"data:{item.mime_type};base64,{item.data}",
                            }
                        )
                if not content:
                    msg_index += 1
                    continue
                messages.append({"role": "user", "content": content})
        elif msg.role == "assistant":
            assistant_msg = msg
            is_different_model = (
                assistant_msg.model != model.id
                and assistant_msg.provider == model.provider
                and assistant_msg.api == model.api
            )
            output: list[dict[str, Any]] = []
            for block in assistant_msg.content:
                if block.type == "thinking":
                    if block.thinking_signature:
                        reasoning_item = json.loads(block.thinking_signature)
                        output.append(reasoning_item)
                elif block.type == "text":
                    parsed_sig = parse_text_signature(block.text_signature)
                    msg_id = parsed_sig.get("id") if parsed_sig else None
                    if not msg_id:
                        msg_id = f"msg_{msg_index}"
                    elif len(msg_id) > 64:
                        msg_id = f"msg_{short_hash(msg_id)}"
                    output.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": sanitize_surrogates(block.text),
                                    "annotations": [],
                                }
                            ],
                            "status": "completed",
                            "id": msg_id,
                            "phase": parsed_sig.get("phase") if parsed_sig else None,
                        }
                    )
                elif block.type == "toolCall":
                    parts = block.id.split("|", 1)
                    call_id = parts[0]
                    item_id_raw = parts[1] if len(parts) > 1 else None
                    item_id = item_id_raw
                    if is_different_model and item_id and item_id.startswith("fc_"):
                        item_id = None
                    output.append(
                        {
                            "type": "function_call",
                            "id": item_id,
                            "call_id": call_id,
                            "name": block.name,
                            "arguments": json.dumps(block.arguments),
                        }
                    )
            if not output:
                msg_index += 1
                continue
            messages.extend(output)
        elif msg.role == "toolResult":
            text_result = "\n".join(c.text for c in msg.content if c.type == "text")
            has_images = any(c.type == "image" for c in msg.content)
            has_text = len(text_result) > 0
            call_id = msg.tool_call_id.split("|", 1)[0]

            if has_images and model.input and "image" in model.input:
                content_parts: list[dict[str, Any]] = []
                if has_text:
                    content_parts.append(
                        {
                            "type": "input_text",
                            "text": sanitize_surrogates(text_result),
                        }
                    )
                for block in msg.content:
                    if block.type == "image":
                        content_parts.append(
                            {
                                "type": "input_image",
                                "detail": "auto",
                                "image_url": f"data:{block.mime_type};base64,{block.data}",
                            }
                        )
                output_value: str | list[dict[str, Any]] = content_parts
            else:
                output_value = sanitize_surrogates(
                    text_result if has_text else "(see attached image)"
                )

            messages.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output_value,
                }
            )
        msg_index += 1

    return messages


# =============================================================================
# Tool conversion
# =============================================================================


def convert_responses_tools(
    tools: list[Tool],
    options: ConvertResponsesToolsOptions | None = None,
) -> list[dict[str, Any]]:
    strict = False
    if options:
        strict = options.get("strict", False)
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "strict": strict,
        }
        for tool in tools
    ]


# =============================================================================
# Stream processing
# =============================================================================


async def process_responses_stream(
    event_stream: AsyncIterable[dict[str, Any]],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    model: ModelInfo,
    options: OpenAIResponsesStreamOptions | None = None,
) -> None:
    current_item: dict[str, Any] | None = None
    current_block: dict[str, Any] | None = None

    async for event in event_stream:
        event_type = event.get("type", "")

        if event_type == "response.created":
            response = event.get("response", {})
            if response.get("id"):
                output.response_id = response["id"]

        elif event_type == "response.output_item.added":
            item = event.get("item", {})
            item_type = item.get("type")
            if item_type == "reasoning":
                current_item = item
                current_block = {"type": "thinking", "thinking": ""}
                output.content.append(current_block)
                stream.push(
                    {
                        "type": "thinking_start",
                        "contentIndex": len(output.content) - 1,
                        "partial": output,
                        "delta": None,
                    }
                )
            elif item_type == "message":
                current_item = item
                current_block = {"type": "text", "text": ""}
                output.content.append(current_block)
                stream.push(
                    {
                        "type": "text_start",
                        "contentIndex": len(output.content) - 1,
                        "partial": output,
                        "delta": None,
                    }
                )
            elif item_type == "function_call":
                current_item = item
                current_block = {
                    "type": "toolCall",
                    "id": f"{item.get('call_id', '')}|{item.get('id', '')}",
                    "name": item.get("name", ""),
                    "arguments": {},
                    "partialJson": item.get("arguments", ""),
                }
                output.content.append(current_block)
                stream.push(
                    {
                        "type": "toolcall_start",
                        "contentIndex": len(output.content) - 1,
                        "partial": output,
                        "delta": None,
                    }
                )

        elif event_type == "response.reasoning_summary_part.added":
            if current_item and current_item.get("type") == "reasoning":
                current_item.setdefault("summary", []).append(event.get("part"))

        elif event_type == "response.reasoning_summary_text.delta":
            if (
                current_item
                and current_item.get("type") == "reasoning"
                and current_block
                and current_block.get("type") == "thinking"
            ):
                current_item.setdefault("summary", [])
                summary = current_item["summary"]
                if summary:
                    last_part = summary[-1]
                    current_block["thinking"] += event["delta"]
                    last_part["text"] = last_part.get("text", "") + event["delta"]
                    stream.push(
                        {
                            "type": "thinking_delta",
                            "contentIndex": len(output.content) - 1,
                            "delta": event["delta"],
                            "partial": output,
                        }
                    )

        elif event_type == "response.reasoning_summary_part.done":
            if (
                current_item
                and current_item.get("type") == "reasoning"
                and current_block
                and current_block.get("type") == "thinking"
            ):
                current_item.setdefault("summary", [])
                summary = current_item["summary"]
                if summary:
                    last_part = summary[-1]
                    current_block["thinking"] += "\n\n"
                    last_part["text"] = last_part.get("text", "") + "\n\n"
                    stream.push(
                        {
                            "type": "thinking_delta",
                            "contentIndex": len(output.content) - 1,
                            "delta": "\n\n",
                            "partial": output,
                        }
                    )

        elif event_type == "response.reasoning_text.delta":
            if (
                current_item
                and current_item.get("type") == "reasoning"
                and current_block
                and current_block.get("type") == "thinking"
            ):
                current_block["thinking"] += event["delta"]
                stream.push(
                    {
                        "type": "thinking_delta",
                        "contentIndex": len(output.content) - 1,
                        "delta": event["delta"],
                        "partial": output,
                    }
                )

        elif event_type == "response.content_part.added":
            if current_item and current_item.get("type") == "message":
                current_item.setdefault("content", [])
                part = event.get("part", {})
                if part.get("type") in ("output_text", "refusal"):
                    current_item["content"].append(part)

        elif event_type == "response.output_text.delta":
            if (
                current_item
                and current_item.get("type") == "message"
                and current_block
                and current_block.get("type") == "text"
            ):
                content = current_item.get("content", [])
                if not content:
                    continue
                last_part = content[-1]
                if last_part.get("type") == "output_text":
                    delta = event["delta"]
                    current_block["text"] += delta
                    last_part["text"] = last_part.get("text", "") + delta
                    stream.push(
                        {
                            "type": "text_delta",
                            "contentIndex": len(output.content) - 1,
                            "delta": delta,
                            "partial": output,
                        }
                    )

        elif event_type == "response.refusal.delta":
            if (
                current_item
                and current_item.get("type") == "message"
                and current_block
                and current_block.get("type") == "text"
            ):
                content = current_item.get("content", [])
                if not content:
                    continue
                last_part = content[-1]
                if last_part.get("type") == "refusal":
                    delta = event["delta"]
                    current_block["text"] += delta
                    last_part["refusal"] = last_part.get("refusal", "") + delta
                    stream.push(
                        {
                            "type": "text_delta",
                            "contentIndex": len(output.content) - 1,
                            "delta": delta,
                            "partial": output,
                        }
                    )

        elif event_type == "response.function_call_arguments.delta":
            if (
                current_item
                and current_item.get("type") == "function_call"
                and current_block
                and current_block.get("type") == "toolCall"
            ):
                delta = event["delta"]
                current_block["partialJson"] += delta
                current_block["arguments"] = parse_streaming_json(
                    current_block["partialJson"]
                )
                stream.push(
                    {
                        "type": "toolcall_delta",
                        "contentIndex": len(output.content) - 1,
                        "delta": delta,
                        "partial": output,
                    }
                )

        elif event_type == "response.function_call_arguments.done":
            if (
                current_item
                and current_item.get("type") == "function_call"
                and current_block
                and current_block.get("type") == "toolCall"
            ):
                previous_partial_json = current_block["partialJson"]
                current_block["partialJson"] = event.get("arguments", "")
                current_block["arguments"] = parse_streaming_json(
                    current_block["partialJson"]
                )

                if event.get("arguments", "").startswith(previous_partial_json):
                    delta = event["arguments"][len(previous_partial_json) :]
                    if delta:
                        stream.push(
                            {
                                "type": "toolcall_delta",
                                "contentIndex": len(output.content) - 1,
                                "delta": delta,
                                "partial": output,
                            }
                        )

        elif event_type == "response.output_item.done":
            item = event.get("item", {})
            item_type = item.get("type")

            if (
                item_type == "reasoning"
                and current_block
                and current_block.get("type") == "thinking"
            ):
                summary_text = "\n\n".join(
                    s.get("text", "") for s in (item.get("summary") or [])
                )
                content_text = "\n\n".join(
                    c.get("text", "") for c in (item.get("content") or [])
                )
                current_block["thinking"] = (
                    summary_text or content_text or current_block["thinking"]
                )
                current_block["thinkingSignature"] = json.dumps(item)
                stream.push(
                    {
                        "type": "thinking_end",
                        "contentIndex": len(output.content) - 1,
                        "content": current_block["thinking"],
                        "partial": output,
                        "delta": None,
                    }
                )
                current_block = None

            elif (
                item_type == "message"
                and current_block
                and current_block.get("type") == "text"
            ):
                content_list = item.get("content", [])
                current_block["text"] = "".join(
                    c.get("text", c.get("refusal", "")) for c in content_list
                )
                current_block["textSignature"] = encode_text_signature_v1(
                    item.get("id", ""),
                    item.get("phase"),
                )
                stream.push(
                    {
                        "type": "text_end",
                        "contentIndex": len(output.content) - 1,
                        "content": current_block["text"],
                        "partial": output,
                        "delta": None,
                    }
                )
                current_block = None

            elif item_type == "function_call":
                if (
                    current_block
                    and current_block.get("type") == "toolCall"
                    and current_block.get("partialJson")
                ):
                    args = parse_streaming_json(current_block["partialJson"])
                else:
                    args = parse_streaming_json(item.get("arguments") or "{}")

                if current_block and current_block.get("type") == "toolCall":
                    current_block["arguments"] = args
                    current_block.pop("partialJson", None)
                    tool_call = current_block
                else:
                    tool_call = {
                        "type": "toolCall",
                        "id": f"{item.get('call_id', '')}|{item.get('id', '')}",
                        "name": item.get("name", ""),
                        "arguments": args,
                    }

                current_block = None
                stream.push(
                    {
                        "type": "toolcall_end",
                        "contentIndex": len(output.content) - 1,
                        "toolCall": tool_call,
                        "partial": output,
                        "delta": None,
                    }
                )

        elif event_type == "response.completed":
            response = event.get("response", {})
            if response.get("id"):
                output.response_id = response["id"]
            if response.get("usage"):
                usage = response["usage"]
                cached_tokens = (
                    usage.get("input_tokens_details", {}).get("cached_tokens", 0)
                    if isinstance(usage.get("input_tokens_details"), dict)
                    else 0
                )
                output.usage.input = (usage.get("input_tokens") or 0) - cached_tokens
                output.usage.output = usage.get("output_tokens") or 0
                output.usage.cache_read = cached_tokens
                output.usage.cache_write = 0
                output.usage.total_tokens = usage.get("total_tokens") or 0
                output.usage.cost.input = 0
                output.usage.cost.output = 0
                output.usage.cost.cache_read = 0
                output.usage.cost.cache_write = 0
                output.usage.cost.total = 0

            calculate_cost(model, output.usage)

            if options:
                apply_svc_tier = options.get("apply_service_tier_pricing")
                if apply_svc_tier:
                    resolve_svc_tier = options.get("resolve_service_tier")
                    if resolve_svc_tier:
                        svc_tier = resolve_svc_tier(
                            response.get("service_tier"),
                            options.get("service_tier"),
                        )
                    else:
                        svc_tier = response.get("service_tier") or options.get(
                            "service_tier"
                        )
                    apply_svc_tier(output.usage, svc_tier)

            output.stop_reason = map_stop_reason(response.get("status"))
            if (
                any(b.get("type") == "toolCall" for b in output.content)
                and output.stop_reason == "stop"
            ):
                output.stop_reason = "toolUse"

        elif event_type == "error":
            code = event.get("code", "unknown")
            message = event.get("message", "Unknown error")
            raise RuntimeError(f"Error Code {code}: {message}")

        elif event_type == "response.failed":
            resp = event.get("response", {})
            error = resp.get("error")
            details = resp.get("incomplete_details")
            if error:
                msg = f"{error.get('code', 'unknown')}: {error.get('message', 'no message')}"
            elif details:
                msg = f"incomplete: {details.get('reason', 'unknown')}"
            else:
                msg = "Unknown error (no error details in response)"
            raise RuntimeError(msg)


# =============================================================================
# Stop reason mapping
# =============================================================================


def map_stop_reason(status: str | None) -> StopReason:
    if not status:
        return "stop"
    mapping: dict[str, StopReason] = {
        "completed": "stop",
        "incomplete": "length",
        "failed": "error",
        "cancelled": "error",
        "in_progress": "stop",
        "queued": "stop",
    }
    result = mapping.get(status)
    if result is not None:
        return result
    raise ValueError(f"Unhandled stop reason: {status}")
