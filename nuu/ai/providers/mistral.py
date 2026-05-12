"""
Mistral Conversations API provider. Streams via the Mistral API with support
for tool calling, thinking, and SSE-based responses.

Owns: stream_mistral(), stream_simple_mistral().
Delegates to: httpx for HTTP streaming.

Data flow: ModelInfo + Context + Options -> Mistral API -> AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.event_stream, httpx
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Callable, Literal

import httpx

from ..api_registry import ApiProvider, register_api_provider
from ..event_stream import AssistantMessageEventStream
from ..models import calculate_cost, clamp_thinking_level
from ..providers.simple_options import build_base_options
from ..types import (
    AssistantMessage,
    Context,
    ImageContent,
    KnownApi,
    Message,
    ModelInfo,
    SimpleStreamOptions,
    StreamOptions,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)

MISTRAL_TOOL_CALL_ID_LENGTH = 9
_MAX_MISTRAL_ERROR_BODY_CHARS = 4000

API_KEY_ENV_MAP: dict[str, str] = {
    "mistral": "MISTRAL_API_KEY",
}


class MistralOptions(StreamOptions):
    tool_choice: Literal["auto", "none", "any", "required"] | dict | None = None
    prompt_mode: Literal["reasoning"] | None = None
    reasoning_effort: Literal["none", "high"] | None = None


def stream_mistral(
    model: ModelInfo,
    context: Context,
    options: MistralOptions | None = None,
) -> AssistantMessageEventStream:
    s = AssistantMessageEventStream()

    async def _run() -> None:
        output = _create_output(model)
        try:
            api_key = ""
            if options and options.api_key:
                api_key = options.api_key
            if not api_key:
                env_key = _get_env_api_key(model.provider)
                if env_key:
                    api_key = env_key
            if not api_key:
                raise ValueError(f"No API key for provider: {model.provider}")

            normalize_id = _create_mistral_tool_call_id_normalizer()
            messages = _transform_messages(context.messages, model, normalize_id)
            payload = _build_chat_payload(model, context, messages, options)
            headers = _build_headers(model, options, api_key)

            url = _build_url(model)
            timeout_ms = (options and options.timeout_ms) or 30000

            s.push({"type": "start", "partial": output})

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_ms / 1000)
            ) as client:
                async with client.stream(
                    "POST", url, json=payload, headers=headers
                ) as response:
                    response.raise_for_status()
                    await _consume_chat_stream(model, output, s, response)

            if output.stop_reason in ("aborted", "error"):
                raise RuntimeError("An unknown error occurred")

            s.push({"type": "done", "reason": output.stop_reason, "message": output})
        except Exception as e:
            for block in output.content:
                if isinstance(block, ToolCall):
                    if hasattr(block, "partial_args"):
                        del block.partial_args
            output.stop_reason = "error"
            output.error_message = _format_mistral_error(e)
            s.push({"type": "error", "reason": "error", "error": output})

    asyncio.create_task(_run())
    return s


def stream_simple_mistral(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = (options and options.api_key) or _get_env_api_key(model.provider)
    if not api_key:
        raise ValueError(f"No API key for provider: {model.provider}")

    base = build_base_options(model, options, api_key)
    clamped = (
        clamp_thinking_level(model, options.reasoning)
        if (options and options.reasoning)
        else None
    )
    reasoning: str | None = None if clamped == "off" else clamped
    should_use_reasoning = model.reasoning and reasoning is not None

    mistral_options = MistralOptions(
        temperature=base.temperature,
        max_tokens=base.max_tokens,
        api_key=base.api_key,
        transport=base.transport,
        cache_retention=base.cache_retention,
        session_id=base.session_id,
        headers=base.headers,
        timeout_ms=base.timeout_ms,
        max_retries=base.max_retries,
        max_retry_delay_ms=base.max_retry_delay_ms,
        metadata=base.metadata,
        prompt_mode="reasoning"
        if should_use_reasoning and _uses_prompt_mode_reasoning(model)
        else None,
        reasoning_effort=_map_reasoning_effort(model, reasoning)
        if should_use_reasoning and _uses_reasoning_effort(model)
        else None,
    )
    return stream_mistral(model, context, mistral_options)


def _get_env_api_key(provider: str) -> str | None:
    env_var = API_KEY_ENV_MAP.get(provider)
    if env_var:
        return os.environ.get(env_var)
    return None


def _create_output(model: ModelInfo) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(
            input=0,
            output=0,
            cache_read=0,
            cache_write=0,
            total_tokens=0,
            cost=UsageCost(input=0, output=0, cache_read=0, cache_write=0, total=0),
        ),
        stop_reason="stop",
        timestamp=int(time.time() * 1000),
    )


def _sanitize_surrogates(text: str) -> str:
    result: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDBFF:
            if i + 1 < len(text) and 0xDC00 <= ord(text[i + 1]) <= 0xDFFF:
                result.append(ch)
                result.append(text[i + 1])
                i += 2
                continue
            i += 1
            continue
        if 0xDC00 <= cp <= 0xDFFF:
            i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _short_hash(s: str) -> str:
    MASK32 = 0xFFFFFFFF
    h1 = 0xDEADBEEF
    h2 = 0x41C6CE57

    for ch in s:
        val = ord(ch)
        h1 = ((h1 ^ val) * 2654435761) & MASK32
        h2 = ((h2 ^ val) * 1597334677) & MASK32

    new_h1_part1 = ((h1 ^ (h1 >> 16)) * 2246822507) & MASK32
    new_h1_part2 = ((h2 ^ (h2 >> 13)) * 3266489909) & MASK32
    new_h1 = (new_h1_part1 ^ new_h1_part2) & MASK32

    new_h2_part1 = ((h2 ^ (h2 >> 16)) * 2246822507) & MASK32
    new_h2_part2 = ((new_h1 ^ (new_h1 >> 13)) * 3266489909) & MASK32
    new_h2 = (new_h2_part1 ^ new_h2_part2) & MASK32

    return _to_base36(new_h2) + _to_base36(new_h1)


def _to_base36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    result: list[str] = []
    while n > 0:
        result.append(chars[n % 36])
        n //= 36
    return "".join(reversed(result))


def _create_mistral_tool_call_id_normalizer() -> Callable[[str], str]:
    id_map: dict[str, str] = {}
    reverse_map: dict[str, str] = {}

    def normalize(original_id: str) -> str:
        existing = id_map.get(original_id)
        if existing:
            return existing

        attempt = 0
        while True:
            candidate = _derive_mistral_tool_call_id(original_id, attempt)
            owner = reverse_map.get(candidate)
            if not owner or owner == original_id:
                id_map[original_id] = candidate
                reverse_map[candidate] = original_id
                return candidate
            attempt += 1

    return normalize


def _derive_mistral_tool_call_id(original_id: str, attempt: int) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]", "", original_id)
    if attempt == 0 and len(normalized) == MISTRAL_TOOL_CALL_ID_LENGTH:
        return normalized
    seed_base = normalized or original_id
    seed = seed_base if attempt == 0 else f"{seed_base}:{attempt}"
    return re.sub(r"[^a-zA-Z0-9]", "", _short_hash(seed))[:MISTRAL_TOOL_CALL_ID_LENGTH]


def _format_mistral_error(error: Exception) -> str:
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        body = _get_error_body(error)
        if body:
            return f"Mistral API error ({status_code}): {body}"
        return f"Mistral API error ({status_code}): {error}"
    return str(error)


def _get_error_body(error: Exception) -> str | None:
    try:
        response = getattr(error, "response", None)
        if response is not None:
            text = response.text.strip()
            return _truncate_error_text(text, _MAX_MISTRAL_ERROR_BODY_CHARS)
    except Exception:
        pass
    return None


def _truncate_error_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated {len(text) - max_chars} chars]"


def _build_url(model: ModelInfo) -> str:
    base = model.base_url.rstrip("/")
    return f"{base}/chat/completions"


def _build_headers(
    model: ModelInfo,
    options: MistralOptions | None,
    api_key: str,
) -> dict[str, str]:
    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    if model.headers:
        headers.update(model.headers)
    if options and options.headers:
        headers.update(options.headers)
    if options and options.session_id and "x-affinity" not in headers:
        headers["x-affinity"] = options.session_id

    return headers


def _transform_messages(
    messages: list[Message],
    model: ModelInfo,
    normalize_id: Callable[[str], str],
) -> list[Message]:
    id_map: dict[str, str] = {}
    supports_images = "image" in model.input
    result: list[Message] = []

    for msg in messages:
        if isinstance(msg, UserMessage):
            if not supports_images and not isinstance(msg.content, str):
                had_images = any(p.type == "image" for p in msg.content)
                if had_images:
                    new_content: list[TextContent | ImageContent] = []
                    last_was_placeholder = False
                    placeholder = "(image omitted: model does not support images)"
                    for part in msg.content:
                        if part.type == "image":
                            if not last_was_placeholder:
                                new_content.append(
                                    TextContent(type="text", text=placeholder)
                                )
                            last_was_placeholder = True
                        else:
                            new_content.append(part)
                            last_was_placeholder = (
                                part.type == "text" and part.text == placeholder
                            )
                    result.append(
                        UserMessage(
                            role="user",
                            content=new_content,
                            timestamp=msg.timestamp,
                        )
                    )
                    continue
            result.append(msg)

        elif isinstance(msg, AssistantMessage):
            new_content: list[TextContent | ThinkingContent | ToolCall] = []
            for block in msg.content:
                if isinstance(block, ToolCall):
                    old_id = block.id
                    new_id = normalize_id(old_id)
                    if new_id != old_id:
                        id_map[old_id] = new_id
                        new_content.append(block.model_copy(update={"id": new_id}))
                    else:
                        new_content.append(block)
                else:
                    new_content.append(block)
            result.append(msg.model_copy(update={"content": new_content}))

        elif isinstance(msg, ToolResultMessage):
            new_id = id_map.get(msg.tool_call_id, msg.tool_call_id)
            if new_id != msg.tool_call_id:
                result.append(msg.model_copy(update={"tool_call_id": new_id}))
            else:
                result.append(msg)

        else:
            result.append(msg)

    return result


def _build_chat_payload(
    model: ModelInfo,
    context: Context,
    messages: list[Message],
    options: MistralOptions | None,
) -> dict[str, Any]:
    supports_images = "image" in model.input
    payload: dict[str, Any] = {
        "model": model.id,
        "stream": True,
        "messages": _to_chat_messages(messages, supports_images),
    }

    if context.tools:
        payload["tools"] = _to_function_tools(context.tools)
    if options and options.temperature is not None:
        payload["temperature"] = options.temperature
    if options and options.max_tokens is not None:
        payload["max_tokens"] = options.max_tokens
    if options and options.tool_choice is not None:
        payload["tool_choice"] = _map_tool_choice(options.tool_choice)
    if options and options.prompt_mode is not None:
        payload["prompt_mode"] = options.prompt_mode
    if options and options.reasoning_effort is not None:
        payload["reasoning_effort"] = options.reasoning_effort

    if context.system_prompt:
        payload["messages"].insert(
            0,
            {
                "role": "system",
                "content": _sanitize_surrogates(context.system_prompt),
            },
        )

    return payload


async def _consume_chat_stream(
    model: ModelInfo,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    response: httpx.Response,
) -> None:
    current_block: TextContent | ThinkingContent | None = None
    tool_blocks_by_key: dict[str, int] = {}
    partial_args: dict[int, str] = {}

    def finish_current_block(
        block: TextContent | ThinkingContent | None = None,
    ) -> None:
        if block is None:
            return
        idx = len(output.content) - 1
        if block.type == "text":
            stream.push(
                {
                    "type": "text_end",
                    "contentIndex": idx,
                    "content": block.text,
                    "partial": output,
                }
            )
        elif block.type == "thinking":
            stream.push(
                {
                    "type": "thinking_end",
                    "contentIndex": idx,
                    "content": block.thinking,
                    "partial": output,
                }
            )

    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue

        data = line[6:].strip()
        if not data or data == "[DONE]":
            continue

        chunk = json.loads(data)
        chunk_id = chunk.get("id")
        if chunk_id and not output.response_id:
            output.response_id = chunk_id

        usage_data = chunk.get("usage")
        if usage_data:
            output.usage.input = usage_data.get("prompt_tokens", 0)
            output.usage.output = usage_data.get("completion_tokens", 0)
            output.usage.cache_read = 0
            output.usage.cache_write = 0
            output.usage.total_tokens = (
                usage_data.get("total_tokens", 0)
                or output.usage.input + output.usage.output
            )
            calculate_cost(model, output.usage)

        choices = chunk.get("choices", [])
        if not choices:
            continue

        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            output.stop_reason = _map_chat_stop_reason(finish_reason)

        delta = choice.get("delta", {})
        delta_content = delta.get("content")

        if delta_content is not None:
            if isinstance(delta_content, str):
                text_delta = _sanitize_surrogates(delta_content)
                if not current_block or current_block.type != "text":
                    finish_current_block(current_block)
                    current_block = TextContent(type="text", text="")
                    output.content.append(current_block)
                    stream.push(
                        {
                            "type": "text_start",
                            "contentIndex": len(output.content) - 1,
                            "partial": output,
                        }
                    )
                current_block.text += text_delta
                stream.push(
                    {
                        "type": "text_delta",
                        "contentIndex": len(output.content) - 1,
                        "delta": text_delta,
                        "partial": output,
                    }
                )
            elif isinstance(delta_content, list):
                for item in delta_content:
                    if isinstance(item, str):
                        text_delta = _sanitize_surrogates(item)
                        if not current_block or current_block.type != "text":
                            finish_current_block(current_block)
                            current_block = TextContent(type="text", text="")
                            output.content.append(current_block)
                            stream.push(
                                {
                                    "type": "text_start",
                                    "contentIndex": len(output.content) - 1,
                                    "partial": output,
                                }
                            )
                        current_block.text += text_delta
                        stream.push(
                            {
                                "type": "text_delta",
                                "contentIndex": len(output.content) - 1,
                                "delta": text_delta,
                                "partial": output,
                            }
                        )
                    elif isinstance(item, dict):
                        item_type = item.get("type")
                        if item_type == "thinking":
                            thinking_parts = item.get("thinking", [])
                            delta_text = "".join(
                                p.get("text", "")
                                for p in thinking_parts
                                if isinstance(p, dict)
                            )
                            thinking_delta = _sanitize_surrogates(delta_text)
                            if not thinking_delta:
                                continue
                            if not current_block or current_block.type != "thinking":
                                finish_current_block(current_block)
                                current_block = ThinkingContent(
                                    type="thinking", thinking=""
                                )
                                output.content.append(current_block)
                                stream.push(
                                    {
                                        "type": "thinking_start",
                                        "contentIndex": len(output.content) - 1,
                                        "partial": output,
                                    }
                                )
                            current_block.thinking += thinking_delta
                            stream.push(
                                {
                                    "type": "thinking_delta",
                                    "contentIndex": len(output.content) - 1,
                                    "delta": thinking_delta,
                                    "partial": output,
                                }
                            )
                        elif item_type == "text":
                            text_delta = _sanitize_surrogates(item.get("text", ""))
                            if not current_block or current_block.type != "text":
                                finish_current_block(current_block)
                                current_block = TextContent(type="text", text="")
                                output.content.append(current_block)
                                stream.push(
                                    {
                                        "type": "text_start",
                                        "contentIndex": len(output.content) - 1,
                                        "partial": output,
                                    }
                                )
                            current_block.text += text_delta
                            stream.push(
                                {
                                    "type": "text_delta",
                                    "contentIndex": len(output.content) - 1,
                                    "delta": text_delta,
                                    "partial": output,
                                }
                            )

        tool_calls = delta.get("tool_calls", [])
        for tc in tool_calls:
            if current_block:
                finish_current_block(current_block)
                current_block = None

            call_id = tc.get("id", "") or ""
            if not call_id or call_id == "null":
                call_id = _derive_mistral_tool_call_id(
                    f"toolcall:{tc.get('index', 0)}", 0
                )

            key = f"{call_id}:{tc.get('index', 0)}"
            existing_index = tool_blocks_by_key.get(key)
            block: ToolCall | None = None

            if existing_index is not None:
                existing = output.content[existing_index]
                if isinstance(existing, ToolCall):
                    block = existing

            if block is None:
                block = ToolCall(
                    type="toolCall",
                    id=call_id,
                    name=tc["function"]["name"],
                    arguments={},
                )
                output.content.append(block)
                content_index = len(output.content) - 1
                tool_blocks_by_key[key] = content_index
                partial_args[content_index] = ""
                stream.push(
                    {
                        "type": "toolcall_start",
                        "contentIndex": content_index,
                        "partial": output,
                    }
                )
            else:
                content_index = existing_index

            args_raw = tc["function"]["arguments"]
            args_delta = (
                json.dumps(args_raw) if not isinstance(args_raw, str) else args_raw
            )
            partial_args[content_index] = (
                partial_args.get(content_index, "") + args_delta
            )
            output.content[content_index].arguments = _parse_streaming_json(
                partial_args[content_index]
            )
            stream.push(
                {
                    "type": "toolcall_delta",
                    "contentIndex": content_index,
                    "delta": args_delta,
                    "partial": output,
                }
            )

    finish_current_block(current_block)
    for idx in tool_blocks_by_key.values():
        block = output.content[idx]
        if not isinstance(block, ToolCall):
            continue
        block.arguments = _parse_streaming_json(partial_args.get(idx, ""))
        stream.push(
            {
                "type": "toolcall_end",
                "contentIndex": idx,
                "toolCall": block,
                "partial": output,
            }
        )


def _to_function_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": _strip_symbol_keys(tool.parameters),
                "strict": False,
            },
        }
        for tool in tools
    ]


def _strip_symbol_keys(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_symbol_keys(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_symbol_keys(entry) for key, entry in value.items()}
    return value


def _to_chat_messages(
    messages: list[Message],
    supports_images: bool,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for msg in messages:
        if isinstance(msg, UserMessage):
            if isinstance(msg.content, str):
                result.append(
                    {"role": "user", "content": _sanitize_surrogates(msg.content)}
                )
                continue

            had_images = any(p.type == "image" for p in msg.content)
            content_items: list[dict[str, Any]] = []
            for part in msg.content:
                if part.type == "text":
                    content_items.append(
                        {"type": "text", "text": _sanitize_surrogates(part.text)}
                    )
                elif part.type == "image":
                    content_items.append(
                        {
                            "type": "image_url",
                            "image_url": f"data:{part.mime_type};base64,{part.data}",
                        }
                    )

            if content_items:
                result.append({"role": "user", "content": content_items})
                continue
            if had_images and not supports_images:
                result.append(
                    {
                        "role": "user",
                        "content": "(image omitted: model does not support images)",
                    }
                )
                continue
            continue

        if isinstance(msg, AssistantMessage):
            content_parts: list[dict[str, Any]] = []
            tool_calls: list[dict[str, Any]] = []

            for block in msg.content:
                if isinstance(block, TextContent):
                    if block.text.strip():
                        content_parts.append(
                            {"type": "text", "text": _sanitize_surrogates(block.text)}
                        )
                elif isinstance(block, ThinkingContent):
                    if block.thinking.strip():
                        content_parts.append(
                            {
                                "type": "thinking",
                                "thinking": [
                                    {
                                        "type": "text",
                                        "text": _sanitize_surrogates(block.thinking),
                                    }
                                ],
                            }
                        )
                elif isinstance(block, ToolCall):
                    tool_calls.append(
                        {
                            "id": block.id,
                            "type": "function",
                            "function": {
                                "name": block.name,
                                "arguments": json.dumps(block.arguments or {}),
                            },
                        }
                    )

            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if content_parts:
                assistant_msg["content"] = content_parts
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            if content_parts or tool_calls:
                result.append(assistant_msg)
            continue

        if isinstance(msg, ToolResultMessage):
            text_parts: list[str] = []
            for part in msg.content:
                if part.type == "text":
                    text_parts.append(_sanitize_surrogates(part.text))
            text_result = "\n".join(text_parts)
            has_images = any(p.type == "image" for p in msg.content)
            tool_text = _build_tool_result_text(
                text_result, has_images, supports_images, msg.is_error
            )

            content_items = [{"type": "text", "text": tool_text}]
            if supports_images:
                for part in msg.content:
                    if part.type == "image":
                        content_items.append(
                            {
                                "type": "image_url",
                                "image_url": f"data:{part.mime_type};base64,{part.data}",
                            }
                        )

            result.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "name": msg.tool_name,
                    "content": content_items,
                }
            )

    return result


def _build_tool_result_text(
    text: str, has_images: bool, supports_images: bool, is_error: bool
) -> str:
    trimmed = text.strip()
    error_prefix = "[tool error] " if is_error else ""

    if trimmed:
        image_suffix = (
            "\n[tool image omitted: model does not support images]"
            if has_images and not supports_images
            else ""
        )
        return f"{error_prefix}{trimmed}{image_suffix}"

    if has_images:
        if supports_images:
            return (
                "[tool error] (see attached image)"
                if is_error
                else "(see attached image)"
            )
        return (
            "[tool error] (image omitted: model does not support images)"
            if is_error
            else "(image omitted: model does not support images)"
        )

    return "[tool error] (no tool output)" if is_error else "(no tool output)"


def _map_chat_stop_reason(reason: str | None) -> str:
    if reason is None:
        return "stop"
    switch = {
        "stop": "stop",
        "length": "length",
        "model_length": "length",
        "tool_calls": "toolUse",
        "error": "error",
    }
    return switch.get(reason, "stop")


def _map_tool_choice(
    choice: Literal["auto", "none", "any", "required"] | dict | None,
) -> str | dict | None:
    if choice is None:
        return None
    if choice in ("auto", "none", "any", "required"):
        return choice
    if isinstance(choice, dict):
        return choice
    return None


def _uses_reasoning_effort(model: ModelInfo) -> bool:
    return model.id in (
        "mistral-small-2603",
        "mistral-small-latest",
        "mistral-medium-3.5",
    )


def _uses_prompt_mode_reasoning(model: ModelInfo) -> bool:
    return model.reasoning and not _uses_reasoning_effort(model)


def _map_reasoning_effort(
    model: ModelInfo,
    level: str,
) -> Literal["none", "high"]:
    if model.thinking_level_map:
        mapped = model.thinking_level_map.get(level)  # type: ignore[arg-type]
        if mapped is not None:
            return "high" if mapped == "high" else "none"
    return "high"


_JSON_ESCAPE_CHARS = frozenset(['"', "\\", "/", "b", "f", "n", "r", "t", "u"])


def _repair_json(text: str) -> str:
    repaired: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        char = text[i]
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            i += 1
            continue

        if char == '"':
            repaired.append(char)
            in_string = False
            i += 1
            continue

        if char == "\\":
            if i + 1 >= len(text):
                repaired.append("\\\\")
                i += 1
                continue
            next_char = text[i + 1]
            if next_char == "u":
                unicode_digits = text[i + 2 : i + 6]
                if len(unicode_digits) == 4 and all(
                    c in "0123456789abcdefABCDEF" for c in unicode_digits
                ):
                    repaired.append(f"\\u{unicode_digits}")
                    i += 6
                    continue
            if next_char in _JSON_ESCAPE_CHARS:
                repaired.append(f"\\{next_char}")
                i += 2
                continue
            repaired.append("\\\\")
            i += 1
            continue

        cp = ord(char)
        if 0x00 <= cp <= 0x1F:
            switch = {
                "\b": "\\b",
                "\f": "\\f",
                "\n": "\\n",
                "\r": "\\r",
                "\t": "\\t",
            }
            if char in switch:
                repaired.append(switch[char])
            else:
                repaired.append(f"\\u{cp:04x}")
        else:
            repaired.append(char)
        i += 1

    return "".join(repaired)


def _parse_streaming_json(text: str) -> dict[str, Any]:
    if not text or text.strip() == "":
        return {}
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        return {}
    except (json.JSONDecodeError, ValueError):
        try:
            repaired = _repair_json(text)
            if repaired != text:
                result = json.loads(repaired)
                if isinstance(result, dict):
                    return result
            return {}
        except (json.JSONDecodeError, ValueError):
            return {}


register_api_provider(
    ApiProvider(
        api=KnownApi.MISTRAL_CONVERSATIONS,
        stream=stream_mistral,
        stream_simple=stream_simple_mistral,
    )
)
