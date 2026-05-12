"""
Anthropic Messages API provider. Streams via SSE, supports thinking blocks,
tool use, and cache retention headers.

Owns: stream_anthropic(), stream_simple_anthropic(), AnthropicOptions.
Delegates to: httpx for HTTP streaming.

Data flow: ModelInfo + Context + Options -> Anthropic SSE -> AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.event_stream, nuu.ai.utils, httpx
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, AsyncIterator, Literal

import httpx

from ..api_registry import ApiProvider, register_api_provider
from ..event_stream import AssistantMessageEventStream
from ..models import calculate_cost
from ..types import (
    AnthropicMessagesCompat,
    AssistantMessage,
    CacheRetention,
    Context,
    ImageContent,
    KnownApi,
    ModelInfo,
    SimpleStreamOptions,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ThinkingLevel,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
FINE_GRAINED_TOOL_STREAMING_BETA = "fine-grained-tool-streaming-2025-05-14"
INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"

CLAUDE_CODE_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Grep",
    "Glob",
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
    "KillShell",
    "NotebookEdit",
    "Skill",
    "Task",
    "TaskOutput",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
]
_CC_TOOL_LOOKUP = {t.lower(): t for t in CLAUDE_CODE_TOOLS}

AnthropicEffort = Literal["low", "medium", "high", "xhigh", "max"]
AnthropicThinkingDisplay = Literal["summarized", "omitted"]


class AnthropicOptions(StreamOptions):
    thinking_enabled: bool | None = None
    thinking_budget_tokens: int | None = None
    effort: AnthropicEffort | None = None
    thinking_display: AnthropicThinkingDisplay | None = None
    interleaved_thinking: bool | None = None
    tool_choice: Literal["auto", "any", "none"] | dict | None = None
    client: httpx.AsyncClient | None = None
    on_payload: Any = None
    on_response: Any = None


def _resolve_cache_retention(
    cache_retention: CacheRetention | None = None,
) -> CacheRetention:
    if cache_retention:
        return cache_retention
    if os.environ.get("PI_CACHE_RETENTION") == "long":
        return "long"
    return "short"


def _get_anthropic_compat(model: ModelInfo) -> AnthropicMessagesCompat:
    compat = model.compat
    if isinstance(compat, AnthropicMessagesCompat):
        return AnthropicMessagesCompat(
            supports_eager_tool_input_streaming=compat.supports_eager_tool_input_streaming
            if compat.supports_eager_tool_input_streaming is not None
            else True,
            supports_long_cache_retention=compat.supports_long_cache_retention
            if compat.supports_long_cache_retention is not None
            else True,
        )
    return AnthropicMessagesCompat(
        supports_eager_tool_input_streaming=True,
        supports_long_cache_retention=True,
    )


def _get_cache_control(
    model: ModelInfo, cache_retention: CacheRetention | None = None
) -> tuple[CacheRetention, dict | None]:
    retention = _resolve_cache_retention(cache_retention)
    if retention == "none":
        return retention, None
    compat = _get_anthropic_compat(model)
    cache_control: dict = {"type": "ephemeral"}
    if retention == "long" and compat.supports_long_cache_retention:
        cache_control["ttl"] = "1h"
    return retention, cache_control


def _to_claude_code_name(name: str) -> str:
    return _CC_TOOL_LOOKUP.get(name.lower(), name)


def _from_claude_code_name(name: str, tools: list[Tool] | None = None) -> str:
    if tools:
        lower_name = name.lower()
        for tool in tools:
            if tool.name.lower() == lower_name:
                return tool.name
    return name


def _normalize_tool_call_id(id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", id)[:64]


def _is_oauth_token(api_key: str) -> bool:
    return "sk-ant-oat" in api_key


def _supports_adaptive_thinking(model_id: str) -> bool:
    return any(
        x in model_id
        for x in [
            "opus-4-6",
            "opus-4.6",
            "opus-4-7",
            "opus-4.7",
            "sonnet-4-6",
            "sonnet-4.6",
        ]
    )


def _map_thinking_level_to_effort(
    model: ModelInfo, level: ThinkingLevel | None
) -> AnthropicEffort:
    if level and model.thinking_level_map:
        mapped = model.thinking_level_map.get(level)
        if isinstance(mapped, str):
            return mapped
    mapping: dict[ThinkingLevel, AnthropicEffort] = {
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "high",
    }
    return mapping.get(level, "high")


def _clamp_reasoning(effort: ThinkingLevel | None) -> ThinkingLevel | None:
    return "high" if effort == "xhigh" else effort


def _adjust_max_tokens_for_thinking(
    base_max_tokens: int,
    model_max_tokens: int,
    reasoning_level: ThinkingLevel,
    custom_budgets: dict | None = None,
) -> tuple[int, int]:
    default_budgets = {"minimal": 1024, "low": 2048, "medium": 8192, "high": 16384}
    budgets = {**default_budgets, **(custom_budgets or {})}
    min_output_tokens = 1024
    level = _clamp_reasoning(reasoning_level) or "high"
    thinking_budget = budgets.get(level, 16384)
    max_tokens = min(base_max_tokens + thinking_budget, model_max_tokens)
    if max_tokens <= thinking_budget:
        thinking_budget = max(0, max_tokens - min_output_tokens)
    return max_tokens, thinking_budget


def _build_base_options(
    model: ModelInfo,
    options: SimpleStreamOptions | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    if options is None:
        return {}
    max_tokens = options.max_tokens
    if max_tokens is None and model.max_tokens > 0:
        max_tokens = min(model.max_tokens, 32000)
    return {
        k: v
        for k, v in {
            "temperature": options.temperature,
            "max_tokens": max_tokens,
            "api_key": api_key or options.api_key,
            "transport": options.transport,
            "cache_retention": options.cache_retention,
            "session_id": options.session_id,
            "headers": options.headers,
            "timeout_ms": options.timeout_ms,
            "max_retries": options.max_retries,
            "max_retry_delay_ms": options.max_retry_delay_ms,
            "metadata": options.metadata,
        }.items()
        if v is not None
    }


def _convert_content_blocks(
    content: list[TextContent | ImageContent],
) -> str | list[dict]:
    has_images = any(c.type == "image" for c in content)
    if not has_images:
        return "\n".join(c.text for c in content)
    blocks: list[dict] = []
    has_text = False
    for c in content:
        if c.type == "text":
            blocks.append({"type": "text", "text": c.text})
            has_text = True
        else:
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": c.mime_type,
                        "data": c.data,
                    },
                }
            )
    if not has_text:
        blocks.insert(0, {"type": "text", "text": "(see attached image)"})
    return blocks


def _should_use_fine_grained_tool_streaming_beta(
    model: ModelInfo, context: Context
) -> bool:
    return (
        bool(context.tools)
        and not _get_anthropic_compat(model).supports_eager_tool_input_streaming
    )


def _map_stop_reason(reason: str) -> str:
    mapping: dict[str, str] = {
        "end_turn": "stop",
        "max_tokens": "length",
        "tool_use": "toolUse",
        "refusal": "error",
        "pause_turn": "stop",
        "stop_sequence": "stop",
        "sensitive": "error",
    }
    if reason in mapping:
        return mapping[reason]
    raise ValueError(f"Unhandled stop reason: {reason}")


def _parse_streaming_json(partial: str) -> dict[str, Any]:
    if not partial:
        return {}
    try:
        return json.loads(partial)
    except json.JSONDecodeError:
        pass
    cleaned = partial.rstrip()
    if cleaned.endswith(","):
        cleaned = cleaned[:-1]
    open_braces = cleaned.count("{") - cleaned.count("}")
    open_brackets = cleaned.count("[") - cleaned.count("]")
    if open_braces > 0:
        cleaned += "}" * open_braces
    if open_brackets > 0:
        cleaned += "]" * open_brackets
    if open_braces > 0 or open_brackets > 0:
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
    return {}


class _SSEEvent:
    def __init__(self, event: str | None, data: str):
        self.event = event
        self.data = data


class _SSEDecoder:
    def __init__(self) -> None:
        self._buffer = ""
        self._event: str | None = None
        self._data: list[str] = []

    def decode(self, chunk: bytes) -> list[_SSEEvent]:
        self._buffer += chunk.decode("utf-8")
        events: list[_SSEEvent] = []
        while True:
            result = self._consume_line()
            if result is None:
                break
            line, rest = result
            self._buffer = rest
            event = self._decode_line(line)
            if event is not None:
                events.append(event)
        return events

    def flush(self) -> list[_SSEEvent]:
        events: list[_SSEEvent] = []
        if self._buffer:
            line = self._buffer
            self._buffer = ""
            event = self._decode_line(line)
            if event is not None:
                events.append(event)
        event = self._flush_event()
        if event is not None:
            events.append(event)
        return events

    def _next_line_break_index(self, text: str) -> int:
        cr = text.find("\r")
        lf = text.find("\n")
        if cr == -1:
            return lf
        if lf == -1:
            return cr
        return min(cr, lf)

    def _consume_line(self) -> tuple[str, str] | None:
        index = self._next_line_break_index(self._buffer)
        if index == -1:
            return None
        next_index = index + 1
        if (
            self._buffer[index] == "\r"
            and next_index < len(self._buffer)
            and self._buffer[next_index] == "\n"
        ):
            next_index += 1
        return self._buffer[:index], self._buffer[next_index:]

    def _decode_line(self, line: str) -> _SSEEvent | None:
        if line == "":
            return self._flush_event()
        if line.startswith(":"):
            return None
        delimiter_index = line.find(":")
        field_name = line if delimiter_index == -1 else line[:delimiter_index]
        value = "" if delimiter_index == -1 else line[delimiter_index + 1 :]
        if value.startswith(" "):
            value = value[1:]
        if field_name == "event":
            self._event = value
        elif field_name == "data":
            self._data.append(value)
        return None

    def _flush_event(self) -> _SSEEvent | None:
        if self._event is None and not self._data:
            return None
        event = _SSEEvent(self._event, "\n".join(self._data))
        self._event = None
        self._data = []
        return event


_ANTHROPIC_MESSAGE_EVENTS = frozenset(
    {
        "message_start",
        "message_delta",
        "message_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
    }
)


async def _iterate_anthropic_events(response: httpx.Response) -> AsyncIterator[dict]:
    decoder = _SSEDecoder()
    saw_message_start = False
    saw_message_stop = False

    async for chunk in response.aiter_bytes():
        for sse in decoder.decode(chunk):
            if sse.event == "error":
                raise RuntimeError(sse.data)
            if sse.event not in _ANTHROPIC_MESSAGE_EVENTS:
                continue
            try:
                event = json.loads(sse.data)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"Could not parse Anthropic SSE event {sse.event}: {e}; data={sse.data}"
                ) from e
            if event["type"] == "message_start":
                saw_message_start = True
            elif event["type"] == "message_stop":
                saw_message_stop = True
            yield event

    for sse in decoder.flush():
        if sse.event == "error":
            raise RuntimeError(sse.data)
        if sse.event not in _ANTHROPIC_MESSAGE_EVENTS:
            continue
        try:
            event = json.loads(sse.data)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Could not parse Anthropic SSE event {sse.event}: {e}; data={sse.data}"
            ) from e
        if event["type"] == "message_start":
            saw_message_start = True
        elif event["type"] == "message_stop":
            saw_message_stop = True
        yield event

    if saw_message_start and not saw_message_stop:
        raise RuntimeError("Anthropic stream ended before message_stop")


def _build_anthropic_client(
    model: ModelInfo,
    api_key: str,
    interleaved_thinking: bool,
    use_fine_grained_tool_streaming_beta: bool,
    options_headers: dict[str, str] | None = None,
    dynamic_headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    needs_interleaved_beta = interleaved_thinking and not _supports_adaptive_thinking(
        model.id
    )
    beta_features: list[str] = []
    if use_fine_grained_tool_streaming_beta:
        beta_features.append(FINE_GRAINED_TOOL_STREAMING_BETA)
    if needs_interleaved_beta:
        beta_features.append(INTERLEAVED_THINKING_BETA)

    headers: dict[str, str | None] = {
        "accept": "application/json",
        "anthropic-dangerous-direct-browser-access": "true",
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if beta_features:
        headers["anthropic-beta"] = ",".join(beta_features)

    base_url = (model.base_url or ANTHROPIC_BASE_URL).rstrip("/")

    if model.provider == "cloudflare-ai-gateway":
        headers["cf-aig-authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = None
        headers["Authorization"] = None
    elif model.provider == "github-copilot":
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = None
    elif _is_oauth_token(api_key):
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = None
        oauth_betas = ["claude-code-20250219", "oauth-2025-04-20"]
        existing_beta = headers.get("anthropic-beta", "")
        all_betas = oauth_betas + ([existing_beta] if existing_beta else [])
        headers["anthropic-beta"] = ",".join(all_betas)
        headers["user-agent"] = "claude-cli/2.1.75"
        headers["x-app"] = "cli"
    else:
        headers["x-api-key"] = api_key

    if model.headers:
        headers.update(model.headers)
    if options_headers:
        headers.update(options_headers)
    if dynamic_headers:
        headers.update(dynamic_headers)

    final_headers = {k: v for k, v in headers.items() if v is not None}
    return httpx.AsyncClient(
        base_url=base_url, headers=final_headers, timeout=httpx.Timeout(600.0)
    )


def _convert_tools(
    tools: list[Tool],
    is_oauth_token: bool,
    supports_eager_tool_input_streaming: bool,
    cache_control: dict | None = None,
) -> list[dict]:
    result: list[dict] = []
    for index, tool in enumerate(tools):
        schema = tool.parameters or {}
        entry: dict = {
            "name": _to_claude_code_name(tool.name) if is_oauth_token else tool.name,
            "description": tool.description,
            "input_schema": {
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", []),
            },
        }
        if supports_eager_tool_input_streaming:
            entry["eager_input_streaming"] = True
        if cache_control and index == len(tools) - 1:
            entry["cache_control"] = cache_control
        result.append(entry)
    return result


def _downgrade_unsupported_images(messages: list, model: ModelInfo) -> list:
    if "image" in model.input:
        return messages
    result: list = []
    for msg in messages:
        if msg.role == "user" and isinstance(msg.content, list):
            new_content: list = []
            had_placeholder = False
            for item in msg.content:
                if item.type == "image":
                    if not had_placeholder:
                        new_content.append(
                            TextContent(
                                type="text",
                                text="(image omitted: model does not support images)",
                            )
                        )
                        had_placeholder = True
                else:
                    new_content.append(item)
                    had_placeholder = (
                        item.text == "(image omitted: model does not support images)"
                    )
            result.append(type(msg)(**{**msg.model_dump(), "content": new_content}))
        elif msg.role == "toolResult":
            new_content: list = []
            had_placeholder = False
            for item in msg.content:
                if item.type == "image":
                    if not had_placeholder:
                        new_content.append(
                            TextContent(
                                type="text",
                                text="(tool image omitted: model does not support images)",
                            )
                        )
                        had_placeholder = True
                else:
                    new_content.append(item)
                    had_placeholder = (
                        item.text
                        == "(tool image omitted: model does not support images)"
                    )
            result.append(type(msg)(**{**msg.model_dump(), "content": new_content}))
        else:
            result.append(msg)
    return result


def _transform_messages(messages: list, model: ModelInfo) -> list:
    tool_call_id_map: dict[str, str] = {}
    image_aware = _downgrade_unsupported_images(messages, model)

    first_pass: list = []
    for msg in image_aware:
        if msg.role == "user":
            first_pass.append(msg)
        elif msg.role == "toolResult":
            norm_id = tool_call_id_map.get(msg.tool_call_id)
            if norm_id and norm_id != msg.tool_call_id:
                first_pass.append(
                    type(msg)(**{**msg.model_dump(), "tool_call_id": norm_id})
                )
            else:
                first_pass.append(msg)
        elif msg.role == "assistant":
            is_same_model = (
                msg.provider == model.provider
                and msg.api == model.api
                and msg.model == model.id
            )
            new_content: list = []
            for block in msg.content:
                if block.type == "thinking":
                    if block.redacted:
                        if is_same_model:
                            new_content.append(block)
                        continue
                    if is_same_model and block.thinking_signature:
                        new_content.append(block)
                    elif not block.thinking or not block.thinking.strip():
                        continue
                    elif is_same_model:
                        new_content.append(block)
                    else:
                        new_content.append(
                            TextContent(type="text", text=block.thinking)
                        )
                elif block.type == "text":
                    new_content.append(block)
                elif block.type == "toolCall":
                    tc_block = block
                    if not is_same_model and tc_block.thought_signature:
                        tc_block = ToolCall(
                            type="toolCall",
                            id=tc_block.id,
                            name=tc_block.name,
                            arguments=tc_block.arguments,
                        )
                    if not is_same_model:
                        norm_id = _normalize_tool_call_id(tc_block.id)
                        if norm_id != tc_block.id:
                            tool_call_id_map[tc_block.id] = norm_id
                            tc_block = ToolCall(
                                type="toolCall",
                                id=norm_id,
                                name=tc_block.name,
                                arguments=tc_block.arguments,
                            )
                    new_content.append(tc_block)
            first_pass.append(
                AssistantMessage(
                    role="assistant",
                    content=new_content,
                    api=msg.api,
                    provider=msg.provider,
                    model=msg.model,
                    usage=msg.usage,
                    stop_reason=msg.stop_reason,
                    timestamp=msg.timestamp,
                )
            )

    result: list = []
    pending_tool_calls: list[str] = []
    existing_tool_result_ids: set[str] = set()

    def _insert_synthetic() -> None:
        nonlocal pending_tool_calls, existing_tool_result_ids
        for tc_id in pending_tool_calls:
            if tc_id not in existing_tool_result_ids:
                result.append(
                    ToolResultMessage(
                        role="toolResult",
                        tool_call_id=tc_id,
                        tool_name="",
                        content=[TextContent(type="text", text="No result provided")],
                        is_error=True,
                        timestamp=int(time.time() * 1000),
                    )
                )
        pending_tool_calls = []
        existing_tool_result_ids = set()

    for msg in first_pass:
        if msg.role == "assistant":
            _insert_synthetic()
            if msg.stop_reason in ("error", "aborted"):
                continue
            tc_ids = [b.id for b in msg.content if b.type == "toolCall"]
            if tc_ids:
                pending_tool_calls = tc_ids
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


def _convert_messages(
    messages: list,
    model: ModelInfo,
    is_oauth_token: bool,
    cache_control: dict | None = None,
) -> list[dict]:
    transformed = _transform_messages(messages, model)
    params: list[dict] = []

    i = 0
    while i < len(transformed):
        msg = transformed[i]
        if msg.role == "user":
            content = msg.content
            if isinstance(content, str):
                if content.strip():
                    params.append({"role": "user", "content": content})
            else:
                blocks: list[dict] = []
                for item in content:
                    if item.type == "text":
                        if item.text.strip():
                            blocks.append({"type": "text", "text": item.text})
                    else:
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": item.mime_type,
                                    "data": item.data,
                                },
                            }
                        )
                if blocks:
                    params.append({"role": "user", "content": blocks})
            i += 1
        elif msg.role == "assistant":
            if msg.stop_reason in ("error", "aborted"):
                i += 1
                continue
            blocks: list[dict] = []
            for block in msg.content:
                if block.type == "text":
                    if block.text.strip():
                        blocks.append({"type": "text", "text": block.text})
                elif block.type == "thinking":
                    if block.redacted:
                        if block.thinking_signature:
                            blocks.append(
                                {
                                    "type": "redacted_thinking",
                                    "data": block.thinking_signature,
                                }
                            )
                        continue
                    if not block.thinking.strip():
                        continue
                    if not block.thinking_signature:
                        blocks.append({"type": "text", "text": block.thinking})
                    else:
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": block.thinking,
                                "signature": block.thinking_signature,
                            }
                        )
                elif block.type == "toolCall":
                    norm_id = _normalize_tool_call_id(block.id)
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": norm_id,
                            "name": _to_claude_code_name(block.name)
                            if is_oauth_token
                            else block.name,
                            "input": block.arguments or {},
                        }
                    )
            if blocks:
                params.append({"role": "assistant", "content": blocks})
            i += 1
        elif msg.role == "toolResult":
            tool_results: list[dict] = []
            j = i
            while j < len(transformed) and transformed[j].role == "toolResult":
                curr = transformed[j]
                norm_id = _normalize_tool_call_id(curr.tool_call_id)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": norm_id,
                        "content": _convert_content_blocks(curr.content),
                        "is_error": curr.is_error,
                    }
                )
                j += 1
            if tool_results:
                params.append({"role": "user", "content": tool_results})
            i = j

    if cache_control and params:
        last = params[-1]
        if last["role"] == "user":
            content = last["content"]
            if isinstance(content, list) and content:
                last_block = content[-1]
                if last_block.get("type") in ("text", "image", "tool_result"):
                    last_block["cache_control"] = cache_control
            elif isinstance(content, str):
                last["content"] = [
                    {"type": "text", "text": content, "cache_control": cache_control}
                ]

    return params


def _build_params(
    model: ModelInfo,
    context: Context,
    is_oauth_token: bool,
    options: AnthropicOptions | None = None,
) -> dict:
    if options:
        retention, cache_control = _get_cache_control(model, options.cache_retention)
    else:
        retention, cache_control = _get_cache_control(model)

    params: dict = {
        "model": model.id,
        "messages": _convert_messages(
            context.messages, model, is_oauth_token, cache_control
        ),
        "max_tokens": (
            options.max_tokens
            if options and options.max_tokens
            else model.max_tokens // 3
        ),
        "stream": True,
    }

    if is_oauth_token:
        system_blocks = [
            {
                "type": "text",
                "text": "You are Claude Code, Anthropic's official CLI for Claude.",
            },
        ]
        if cache_control:
            system_blocks[0]["cache_control"] = cache_control
        if context.system_prompt:
            system_blocks.append({"type": "text", "text": context.system_prompt})
            if cache_control:
                system_blocks[-1]["cache_control"] = cache_control
        params["system"] = system_blocks
    elif context.system_prompt:
        system_block: dict = {"type": "text", "text": context.system_prompt}
        if cache_control:
            system_block["cache_control"] = cache_control
        params["system"] = [system_block]

    if options and options.temperature is not None and not options.thinking_enabled:
        params["temperature"] = options.temperature

    if context.tools:
        compat = _get_anthropic_compat(model)
        params["tools"] = _convert_tools(
            context.tools,
            is_oauth_token,
            compat.supports_eager_tool_input_streaming,
            cache_control,
        )

    if options and model.reasoning:
        if options.thinking_enabled:
            display = options.thinking_display or "summarized"
            if _supports_adaptive_thinking(model.id):
                params["thinking"] = {"type": "adaptive", "display": display}
                if options.effort:
                    params["output_config"] = {"effort": options.effort}
            else:
                params["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": options.thinking_budget_tokens or 1024,
                    "display": display,
                }
        elif options.thinking_enabled is False:
            params["thinking"] = {"type": "disabled"}

    if options and options.metadata:
        user_id = options.metadata.get("user_id")
        if user_id:
            params["metadata"] = {"user_id": user_id}

    if options and options.tool_choice:
        if isinstance(options.tool_choice, str):
            params["tool_choice"] = {"type": options.tool_choice}
        else:
            params["tool_choice"] = options.tool_choice

    return params


def stream_anthropic(
    model: ModelInfo,
    context: Context,
    options: AnthropicOptions | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def _run() -> None:
        output = AssistantMessage(
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
                cost=UsageCost(
                    input=0.0, output=0.0, cache_read=0.0, cache_write=0.0, total=0.0
                ),
            ),
            stop_reason="stop",
            timestamp=int(time.time() * 1000),
        )

        try:
            client: httpx.AsyncClient
            is_oauth: bool

            if options and options.client:
                client = options.client
                is_oauth = False
            else:
                api_key = (options.api_key if options else None) or ""
                dynamic_headers = None
                if model.provider == "github-copilot":
                    from .github_copilot_headers import (
                        build_copilot_dynamic_headers,
                        has_copilot_vision_input,
                    )

                    has_images = has_copilot_vision_input(context.messages)
                    dynamic_headers = build_copilot_dynamic_headers(
                        context.messages, has_images
                    )

                interleaved = True
                if options and options.interleaved_thinking is not None:
                    interleaved = options.interleaved_thinking

                client = _build_anthropic_client(
                    model,
                    api_key,
                    interleaved,
                    _should_use_fine_grained_tool_streaming_beta(model, context),
                    (options.headers if options else None),
                    dynamic_headers,
                )
                is_oauth = _is_oauth_token(api_key)

            params = _build_params(model, context, is_oauth, options)

            if options and options.on_payload:
                payload_result = options.on_payload(params, model)
                if asyncio.iscoroutine(payload_result):
                    payload_result = await payload_result
                if payload_result is not None:
                    params = payload_result

            base_url = str(client.base_url).rstrip("/")
            url = f"{base_url}/messages"

            request_kwargs: dict = {}
            if options and options.timeout_ms is not None:
                request_kwargs["timeout"] = httpx.Timeout(options.timeout_ms / 1000.0)

            response = await client.post(url, json=params, **request_kwargs)

            if options and options.on_response:
                response_result = options.on_response(
                    {"status": response.status_code, "headers": dict(response.headers)},
                    model,
                )
                if asyncio.iscoroutine(response_result):
                    await response_result

            stream.push(
                {
                    "type": "start",
                    "contentIndex": None,
                    "delta": None,
                    "partial": output,
                }
            )

            async for event in _iterate_anthropic_events(response):
                event_type = event["type"]

                if event_type == "message_start":
                    msg = event["message"]
                    output.response_id = msg.get("id")
                    usage = msg.get("usage", {})
                    output.usage.input = usage.get("input_tokens", 0)
                    output.usage.output = usage.get("output_tokens", 0)
                    output.usage.cache_read = usage.get("cache_read_input_tokens", 0)
                    output.usage.cache_write = usage.get(
                        "cache_creation_input_tokens", 0
                    )
                    output.usage.total_tokens = (
                        output.usage.input
                        + output.usage.output
                        + output.usage.cache_read
                        + output.usage.cache_write
                    )
                    calculate_cost(model, output.usage)

                elif event_type == "content_block_start":
                    index = event["index"]
                    cb = event["content_block"]
                    cb_type = cb["type"]

                    if cb_type == "text":
                        block = TextContent(type="text", text="")
                        block._index = index
                        output.content.append(block)
                        stream.push(
                            {
                                "type": "text_start",
                                "contentIndex": len(output.content) - 1,
                                "delta": None,
                                "partial": output,
                            }
                        )
                    elif cb_type == "thinking":
                        block = ThinkingContent(
                            type="thinking", thinking="", thinking_signature=""
                        )
                        block._index = index
                        output.content.append(block)
                        stream.push(
                            {
                                "type": "thinking_start",
                                "contentIndex": len(output.content) - 1,
                                "delta": None,
                                "partial": output,
                            }
                        )
                    elif cb_type == "redacted_thinking":
                        block = ThinkingContent(
                            type="thinking",
                            thinking="[Reasoning redacted]",
                            thinking_signature=cb.get("data", ""),
                            redacted=True,
                        )
                        block._index = index
                        output.content.append(block)
                        stream.push(
                            {
                                "type": "thinking_start",
                                "contentIndex": len(output.content) - 1,
                                "delta": None,
                                "partial": output,
                            }
                        )
                    elif cb_type == "tool_use":
                        tool_name = cb["name"]
                        if is_oauth:
                            tool_name = _from_claude_code_name(tool_name, context.tools)
                        block = ToolCall(
                            type="toolCall",
                            id=cb["id"],
                            name=tool_name,
                            arguments=cb.get("input", {}) or {},
                        )
                        block._index = index
                        block._partial_json = ""
                        output.content.append(block)
                        stream.push(
                            {
                                "type": "toolcall_start",
                                "contentIndex": len(output.content) - 1,
                                "delta": None,
                                "partial": output,
                            }
                        )

                elif event_type == "content_block_delta":
                    delta = event["delta"]
                    delta_type = delta["type"]
                    event_index = event["index"]

                    block_idx = None
                    for i, b in enumerate(output.content):
                        if getattr(b, "_index", None) == event_index:
                            block_idx = i
                            break

                    if block_idx is None:
                        continue

                    block = output.content[block_idx]

                    if delta_type == "text_delta":
                        if block.type == "text":
                            block.text += delta["text"]
                            stream.push(
                                {
                                    "type": "text_delta",
                                    "contentIndex": block_idx,
                                    "delta": delta["text"],
                                    "partial": output,
                                }
                            )
                    elif delta_type == "thinking_delta":
                        if block.type == "thinking":
                            block.thinking += delta["thinking"]
                            stream.push(
                                {
                                    "type": "thinking_delta",
                                    "contentIndex": block_idx,
                                    "delta": delta["thinking"],
                                    "partial": output,
                                }
                            )
                    elif delta_type == "input_json_delta":
                        if block.type == "toolCall":
                            partial = (
                                getattr(block, "_partial_json", "")
                                + delta["partial_json"]
                            )
                            block._partial_json = partial
                            block.arguments = _parse_streaming_json(partial)
                            stream.push(
                                {
                                    "type": "toolcall_delta",
                                    "contentIndex": block_idx,
                                    "delta": delta["partial_json"],
                                    "partial": output,
                                }
                            )
                    elif delta_type == "signature_delta":
                        if block.type == "thinking":
                            sig = block.thinking_signature or ""
                            block.thinking_signature = sig + delta["signature"]

                elif event_type == "content_block_stop":
                    event_index = event["index"]
                    block_idx = None
                    for i, b in enumerate(output.content):
                        if getattr(b, "_index", None) == event_index:
                            block_idx = i
                            break

                    if block_idx is None:
                        continue

                    block = output.content[block_idx]
                    if hasattr(block, "_index"):
                        del block._index

                    if block.type == "text":
                        stream.push(
                            {
                                "type": "text_end",
                                "contentIndex": block_idx,
                                "delta": None,
                                "content": block.text,
                                "partial": output,
                            }
                        )
                    elif block.type == "thinking":
                        stream.push(
                            {
                                "type": "thinking_end",
                                "contentIndex": block_idx,
                                "delta": None,
                                "content": block.thinking,
                                "partial": output,
                            }
                        )
                    elif block.type == "toolCall":
                        block.arguments = _parse_streaming_json(
                            getattr(block, "_partial_json", "")
                        )
                        if hasattr(block, "_partial_json"):
                            del block._partial_json
                        stream.push(
                            {
                                "type": "toolcall_end",
                                "contentIndex": block_idx,
                                "toolCall": block,
                                "partial": output,
                            }
                        )

                elif event_type == "message_delta":
                    delta = event["delta"]
                    if delta.get("stop_reason"):
                        output.stop_reason = _map_stop_reason(delta["stop_reason"])

                    usage = event.get("usage", {})
                    if usage.get("input_tokens") is not None:
                        output.usage.input = usage["input_tokens"]
                    if usage.get("output_tokens") is not None:
                        output.usage.output = usage["output_tokens"]
                    if usage.get("cache_read_input_tokens") is not None:
                        output.usage.cache_read = usage["cache_read_input_tokens"]
                    if usage.get("cache_creation_input_tokens") is not None:
                        output.usage.cache_write = usage["cache_creation_input_tokens"]
                    output.usage.total_tokens = (
                        output.usage.input
                        + output.usage.output
                        + output.usage.cache_read
                        + output.usage.cache_write
                    )
                    calculate_cost(model, output.usage)

            if output.stop_reason in ("aborted", "error"):
                raise RuntimeError("An unknown error occurred")

            stream.push(
                {"type": "done", "reason": output.stop_reason, "message": output}
            )
            stream.end()

        except Exception as e:
            for block in output.content:
                if hasattr(block, "_index"):
                    del block._index
                if hasattr(block, "_partial_json"):
                    del block._partial_json

            output.stop_reason = "error"
            output.error_message = str(e)
            stream.push({"type": "error", "reason": "error", "error": output})
            stream.end()

    asyncio.create_task(_run())
    return stream


def stream_simple_anthropic(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = options.api_key if options else None
    base = _build_base_options(model, options, api_key)
    anthropic_options = AnthropicOptions(**base)

    if not options or not options.reasoning:
        anthropic_options.thinking_enabled = False
        return stream_anthropic(model, context, anthropic_options)

    if _supports_adaptive_thinking(model.id):
        effort = _map_thinking_level_to_effort(model, options.reasoning)
        anthropic_options.thinking_enabled = True
        anthropic_options.effort = effort
        return stream_anthropic(model, context, anthropic_options)

    tb = options.thinking_budgets
    custom_budgets = tb.model_dump() if tb else None
    adjusted_max_tokens, adjusted_budget = _adjust_max_tokens_for_thinking(
        anthropic_options.max_tokens or 0,
        model.max_tokens,
        options.reasoning,
        custom_budgets,
    )

    anthropic_options.max_tokens = adjusted_max_tokens
    anthropic_options.thinking_enabled = True
    anthropic_options.thinking_budget_tokens = adjusted_budget

    return stream_anthropic(model, context, anthropic_options)


def register_anthropic_provider() -> None:
    register_api_provider(
        ApiProvider(
            api=KnownApi.ANTHROPIC_MESSAGES,
            stream=stream_anthropic,
            stream_simple=stream_simple_anthropic,
        )
    )
