"""
Pi proxy stream client. Connects to a remote Pi proxy server via HTTP SSE,
translating proxy-specific events into the standard AssistantMessageEvent format.

Owns: stream_proxy(), process_proxy_event(), ProxyStreamOptions.
Delegates to: httpx for HTTP streaming, parse_streaming_json for tool args.

Data flow: ModelInfo + Context + ProxyStreamOptions -> proxy SSE events ->
  process_proxy_event() -> AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.event_stream, nuu.ai.utils.json_parse, httpx
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal, TypedDict, Union

import httpx

from ..ai.event_stream import AssistantMessageEventStream
from ..ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    CacheRetention,
    Context,
    DoneEvent,
    ErrorEvent,
    ModelInfo,
    StartEvent,
    StopReason,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingBudgets,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingLevel,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Transport,
    Usage,
    UsageCost,
)
from ..ai.utils.json_parse import parse_streaming_json


class ProxyStartEvent(TypedDict):
    type: Literal["start"]


class ProxyTextStartEvent(TypedDict):
    type: Literal["text_start"]
    contentIndex: int


class ProxyTextDeltaEvent(TypedDict):
    type: Literal["text_delta"]
    contentIndex: int
    delta: str


class ProxyTextEndEvent(TypedDict):
    type: Literal["text_end"]
    contentIndex: int
    contentSignature: str | None


class ProxyThinkingStartEvent(TypedDict):
    type: Literal["thinking_start"]
    contentIndex: int


class ProxyThinkingDeltaEvent(TypedDict):
    type: Literal["thinking_delta"]
    contentIndex: int
    delta: str


class ProxyThinkingEndEvent(TypedDict):
    type: Literal["thinking_end"]
    contentIndex: int
    contentSignature: str | None


class ProxyToolCallStartEvent(TypedDict):
    type: Literal["toolcall_start"]
    contentIndex: int
    id: str
    toolName: str


class ProxyToolCallDeltaEvent(TypedDict):
    type: Literal["toolcall_delta"]
    contentIndex: int
    delta: str


class ProxyToolCallEndEvent(TypedDict):
    type: Literal["toolcall_end"]
    contentIndex: int


class ProxyDoneEvent(TypedDict):
    type: Literal["done"]
    reason: Literal["stop", "length", "toolUse"]
    usage: Usage


class ProxyErrorEvent(TypedDict):
    type: Literal["error"]
    reason: Literal["aborted", "error"]
    errorMessage: str | None
    usage: Usage


ProxyAssistantMessageEvent = Union[
    ProxyStartEvent,
    ProxyTextStartEvent,
    ProxyTextDeltaEvent,
    ProxyTextEndEvent,
    ProxyThinkingStartEvent,
    ProxyThinkingDeltaEvent,
    ProxyThinkingEndEvent,
    ProxyToolCallStartEvent,
    ProxyToolCallDeltaEvent,
    ProxyToolCallEndEvent,
    ProxyDoneEvent,
    ProxyErrorEvent,
]


class ProxyStreamOptions(TypedDict, total=False):
    signal: asyncio.Event | None
    auth_token: str
    proxy_url: str
    temperature: float | None
    max_tokens: int | None
    reasoning: ThinkingLevel | None
    cache_retention: CacheRetention | None
    session_id: str | None
    headers: dict[str, str] | None
    metadata: dict[str, Any] | None
    transport: Transport | None
    thinking_budgets: ThinkingBudgets | None
    max_retry_delay_ms: int | None


def _build_proxy_request_options(options: ProxyStreamOptions) -> dict[str, Any]:
    result: dict[str, Any] = {}
    camel_map = {
        "temperature": "temperature",
        "max_tokens": "maxTokens",
        "reasoning": "reasoning",
        "cache_retention": "cacheRetention",
        "session_id": "sessionId",
        "headers": "headers",
        "metadata": "metadata",
        "transport": "transport",
        "max_retry_delay_ms": "maxRetryDelayMs",
    }
    for snake, camel in camel_map.items():
        if snake in options:
            result[camel] = options[snake]
    if "thinking_budgets" in options and options["thinking_budgets"] is not None:
        result["thinkingBudgets"] = options["thinking_budgets"].model_dump(
            by_alias=True
        )
    return result


def process_proxy_event(
    proxy_event: ProxyAssistantMessageEvent,
    partial: AssistantMessage,
    _tool_partial_jsons: dict[int, str] | None = None,
) -> tuple[AssistantMessageEvent | None, dict[int, str] | None]:
    if _tool_partial_jsons is None:
        _tool_partial_jsons = {}

    proxy_type = proxy_event["type"]

    if proxy_type == "start":
        return (StartEvent(type="start", partial=partial), _tool_partial_jsons)

    if proxy_type == "text_start":
        content_index = proxy_event["contentIndex"]
        partial.content[content_index] = TextContent(type="text", text="")
        return (
            TextStartEvent(
                type="text_start", contentIndex=content_index, partial=partial
            ),
            _tool_partial_jsons,
        )

    if proxy_type == "text_delta":
        content_index = proxy_event["contentIndex"]
        content = partial.content[content_index]
        if content.type == "text":
            content.text += proxy_event["delta"]
            return (
                TextDeltaEvent(
                    type="text_delta",
                    contentIndex=content_index,
                    delta=proxy_event["delta"],
                    partial=partial,
                ),
                _tool_partial_jsons,
            )
        raise ValueError("Received text_delta for non-text content")

    if proxy_type == "text_end":
        content_index = proxy_event["contentIndex"]
        content = partial.content[content_index]
        if content.type == "text":
            content.text_signature = proxy_event.get("contentSignature")
            return (
                TextEndEvent(
                    type="text_end",
                    contentIndex=content_index,
                    content=content.text,
                    partial=partial,
                ),
                _tool_partial_jsons,
            )
        raise ValueError("Received text_end for non-text content")

    if proxy_type == "thinking_start":
        content_index = proxy_event["contentIndex"]
        partial.content[content_index] = ThinkingContent(type="thinking", thinking="")
        return (
            ThinkingStartEvent(
                type="thinking_start", contentIndex=content_index, partial=partial
            ),
            _tool_partial_jsons,
        )

    if proxy_type == "thinking_delta":
        content_index = proxy_event["contentIndex"]
        content = partial.content[content_index]
        if content.type == "thinking":
            content.thinking += proxy_event["delta"]
            return (
                ThinkingDeltaEvent(
                    type="thinking_delta",
                    contentIndex=content_index,
                    delta=proxy_event["delta"],
                    partial=partial,
                ),
                _tool_partial_jsons,
            )
        raise ValueError("Received thinking_delta for non-thinking content")

    if proxy_type == "thinking_end":
        content_index = proxy_event["contentIndex"]
        content = partial.content[content_index]
        if content.type == "thinking":
            content.thinking_signature = proxy_event.get("contentSignature")
            return (
                ThinkingEndEvent(
                    type="thinking_end",
                    contentIndex=content_index,
                    content=content.thinking,
                    partial=partial,
                ),
                _tool_partial_jsons,
            )
        raise ValueError("Received thinking_end for non-thinking content")

    if proxy_type == "toolcall_start":
        content_index = proxy_event["contentIndex"]
        tool_call = ToolCall(
            type="toolCall",
            id=proxy_event["id"],
            name=proxy_event["toolName"],
            arguments={},
        )
        partial.content[content_index] = tool_call
        _tool_partial_jsons[content_index] = ""
        return (
            ToolCallStartEvent(
                type="toolcall_start", contentIndex=content_index, partial=partial
            ),
            _tool_partial_jsons,
        )

    if proxy_type == "toolcall_delta":
        content_index = proxy_event["contentIndex"]
        content = partial.content[content_index]
        if content.type == "toolCall":
            partial_json = _tool_partial_jsons.get(content_index, "")
            partial_json += proxy_event["delta"]
            _tool_partial_jsons[content_index] = partial_json
            parsed = parse_streaming_json(partial_json) or {}
            content.arguments = parsed
            return (
                ToolCallDeltaEvent(
                    type="toolcall_delta",
                    contentIndex=content_index,
                    delta=proxy_event["delta"],
                    partial=partial,
                ),
                _tool_partial_jsons,
            )
        raise ValueError("Received toolcall_delta for non-toolCall content")

    if proxy_type == "toolcall_end":
        content_index = proxy_event["contentIndex"]
        content = partial.content[content_index]
        if content.type == "toolCall":
            _tool_partial_jsons.pop(content_index, None)
            return (
                ToolCallEndEvent(
                    type="toolcall_end",
                    contentIndex=content_index,
                    toolCall=content,
                    partial=partial,
                ),
                _tool_partial_jsons,
            )
        return (None, _tool_partial_jsons)

    if proxy_type == "done":
        partial.stop_reason = proxy_event["reason"]
        partial.usage = proxy_event["usage"]
        return (
            DoneEvent(type="done", reason=proxy_event["reason"], message=partial),
            _tool_partial_jsons,
        )

    if proxy_type == "error":
        partial.stop_reason = proxy_event["reason"]
        partial.error_message = proxy_event.get("errorMessage")
        partial.usage = proxy_event["usage"]
        return (
            ErrorEvent(type="error", reason=proxy_event["reason"], error=partial),
            _tool_partial_jsons,
        )

    return (None, _tool_partial_jsons)


def stream_proxy(
    model: ModelInfo,
    context: Context,
    options: ProxyStreamOptions,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def _run():
        signal = options.get("signal")
        auth_token = options.get("auth_token", "")
        proxy_url = options.get("proxy_url", "").rstrip("/")

        partial = AssistantMessage(
            role="assistant",
            stop_reason="stop",
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
            timestamp=int(time.time() * 1000),
        )
        _tool_partial_jsons: dict[int, str] = {}
        request_cancelled = False

        try:
            if signal is not None and signal.is_set():
                request_cancelled = True
                raise Exception("Request aborted by user")

            body: dict[str, Any] = {
                "model": model.model_dump(by_alias=True),
                "context": context.model_dump(by_alias=True),
                "options": _build_proxy_request_options(options),
            }

            headers: dict[str, str] = {
                "Authorization": f"Bearer {auth_token}",
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    f"{proxy_url}/api/stream",
                    json=body,
                    headers=headers,
                ) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        error_message = f"Proxy error: {response.status_code}"
                        try:
                            error_data = json.loads(error_body)
                            if "error" in error_data:
                                error_message = f"Proxy error: {error_data['error']}"
                        except (json.JSONDecodeError, ValueError):
                            pass
                        raise Exception(error_message)

                    buffer = ""
                    async for chunk in response.aiter_bytes():
                        if signal is not None and signal.is_set():
                            request_cancelled = True
                            raise Exception("Request aborted by user")

                        buffer += chunk.decode()
                        lines = buffer.split("\n")
                        buffer = lines.pop() or ""

                        for line in lines:
                            if line.startswith("data: "):
                                data = line[6:].strip()
                                if data:
                                    proxy_event: ProxyAssistantMessageEvent = (
                                        json.loads(  # type: ignore
                                            data
                                        )
                                    )
                                    event, _tool_partial_jsons = process_proxy_event(
                                        proxy_event, partial, _tool_partial_jsons
                                    )
                                    if event is not None:
                                        stream.push(event)

            stream.end()
        except Exception as e:
            error_message = str(e)
            reason: StopReason = "aborted" if request_cancelled else "error"
            partial.stop_reason = reason
            partial.error_message = error_message
            stream.push(
                ErrorEvent(type="error", reason=reason, error=partial)  # type: ignore
            )
            stream.end()

    asyncio.create_task(_run())
    return stream
