"""
OpenAI Responses API provider. Implements the newer OpenAI Responses endpoint
with support for tool calling, thinking, and SSE-based streaming.

Owns: stream_openai_responses(), stream_simple_openai_responses().
Delegates to: httpx for HTTP streaming.

Data flow: ModelInfo + Context + Options -> OpenAI Responses API ->
  AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.event_stream, httpx
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any, TypedDict

import httpx

from ..api_registry import ApiProvider, register_api_provider
from ..event_stream import AssistantMessageEventStream
from ..models import clamp_thinking_level
from ..types import (
    AssistantMessage,
    CacheRetention,
    Context,
    ModelInfo,
    OpenAIResponsesCompat,
    SimpleStreamOptions,
    Usage,
    UsageCost,
)
from .github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
)
from .openai_responses_shared import (
    OpenAIResponsesStreamOptions,
    convert_responses_messages,
    convert_responses_tools,
    process_responses_stream,
)

OPENAI_TOOL_CALL_PROVIDERS = frozenset(
    {
        "openai",
        "openai-codex",
        "opencode",
    }
)


class OpenAIResponsesOptions(TypedDict, total=False):
    temperature: float | None
    max_tokens: int | None
    api_key: str | None
    cache_retention: CacheRetention | None
    session_id: str | None
    headers: dict[str, str] | None
    timeout_ms: int | None
    max_retries: int | None
    max_retry_delay_ms: int | None
    metadata: dict[str, Any] | None
    signal: Any
    on_payload: Any
    on_response: Any
    reasoning_effort: str | None
    reasoning_summary: str | None
    service_tier: str | None


def resolve_cache_retention(
    cache_retention: CacheRetention | None = None,
) -> CacheRetention:
    if cache_retention:
        return cache_retention
    env_retention = os.environ.get("PI_CACHE_RETENTION")
    if env_retention == "long":
        return "long"
    return "short"


def get_compat(model: ModelInfo) -> OpenAIResponsesCompat:
    compat = model.compat
    if isinstance(compat, OpenAIResponsesCompat):
        return OpenAIResponsesCompat(
            send_session_id_header=(
                compat.send_session_id_header
                if compat.send_session_id_header is not None
                else True
            ),
            supports_long_cache_retention=(
                compat.supports_long_cache_retention
                if compat.supports_long_cache_retention is not None
                else True
            ),
        )
    return OpenAIResponsesCompat(
        send_session_id_header=True,
        supports_long_cache_retention=True,
    )


def get_prompt_cache_retention(
    compat: OpenAIResponsesCompat,
    cache_retention: CacheRetention,
) -> str | None:
    if cache_retention == "long" and compat.supports_long_cache_retention:
        return "24h"
    return None


async def _iter_sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    buffer = b""
    async for chunk in response.aiter_bytes():
        buffer += chunk
        while b"\n\n" in buffer:
            raw_message, buffer = buffer.split(b"\n\n", 1)
            data_line = None
            for line in raw_message.decode("utf-8").split("\n"):
                if line.startswith("data: "):
                    data_line = line[6:]
                    break
            if data_line is None:
                continue
            data_line = data_line.strip()
            if data_line == "[DONE]":
                continue
            if data_line:
                yield json.loads(data_line)


def stream_openai_responses(
    model: ModelInfo,
    context: Context,
    options: OpenAIResponsesOptions | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def _run():
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
                cost=UsageCost(input=0, output=0, cache_read=0, cache_write=0, total=0),
            ),
            stop_reason="stop",
            timestamp=int(time.time() * 1000),
        )

        try:
            api_key = (options.get("api_key") if options else None) or ""
            cache_retention = resolve_cache_retention(
                options.get("cache_retention") if options else None
            )
            cache_session_id = (
                None
                if cache_retention == "none"
                else (options.get("session_id") if options else None)
            )
            client = _create_client(
                model,
                context,
                api_key,
                options.get("headers") if options else None,
                cache_session_id,
            )
            params = _build_params(model, context, options)

            on_payload = options.get("on_payload") if options else None
            if on_payload:
                next_params = on_payload(params, model)
                if next_params is not None:
                    params = next_params

            request_kwargs: dict[str, Any] = {}
            if options:
                timeout_ms = options.get("timeout_ms")
                if timeout_ms is not None:
                    request_kwargs["timeout"] = timeout_ms / 1000.0

            async with client.stream(
                "POST",
                "/v1/responses",
                json=params,
                **request_kwargs,
            ) as response:
                on_response = options.get("on_response") if options else None
                if on_response:
                    on_response(
                        {
                            "status": response.status_code,
                            "headers": dict(response.headers),
                        },
                        model,
                    )

                stream.push(
                    {
                        "type": "start",
                        "partial": output,
                        "contentIndex": None,
                        "delta": None,
                    }
                )

                svc_options: OpenAIResponsesStreamOptions | None = None
                if options:
                    svc_options = OpenAIResponsesStreamOptions(
                        service_tier=options.get("service_tier"),
                        apply_service_tier_pricing=lambda u, s: (
                            _apply_service_tier_pricing(u, s, model)
                        ),
                    )

                await process_responses_stream(
                    _iter_sse_events(response),
                    output,
                    stream,
                    model,
                    svc_options,
                )

            if options and options.get("signal") and options["signal"].aborted:
                raise RuntimeError("Request was aborted")

            if output.stop_reason in ("aborted", "error"):
                raise RuntimeError("An unknown error occurred")

            stream.push(
                {"type": "done", "reason": output.stop_reason, "message": output}
            )
            stream.end()

        except Exception as error:
            for block in output.content:
                if isinstance(block, dict):
                    block.pop("index", None)
                    block.pop("partialJson", None)

            if options and options.get("signal") and options["signal"].aborted:
                output.stop_reason = "aborted"
            else:
                output.stop_reason = "error"
            output.error_message = str(error)
            stream.push(
                {
                    "type": "error",
                    "reason": output.stop_reason,
                    "error": output,
                }
            )
            stream.end()

    asyncio.create_task(_run())
    return stream


def stream_simple_openai_responses(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = (options.api_key if options else None) or ""
    if not api_key:
        env_var = f"{model.provider.upper()}_API_KEY"
        api_key = os.environ.get(env_var, "")

    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    opt: OpenAIResponsesOptions = {}
    if options:
        if options.max_tokens:
            opt["max_tokens"] = min(options.max_tokens, 32000)
        if options.temperature is not None:
            opt["temperature"] = options.temperature
        if options.cache_retention:
            opt["cache_retention"] = options.cache_retention
        if options.session_id:
            opt["session_id"] = options.session_id
        if options.headers:
            opt["headers"] = options.headers
        if options.timeout_ms is not None:
            opt["timeout_ms"] = options.timeout_ms
        if options.max_retries is not None:
            opt["max_retries"] = options.max_retries
        if options.max_retry_delay_ms is not None:
            opt["max_retry_delay_ms"] = options.max_retry_delay_ms
        if options.reasoning:
            clamped = clamp_thinking_level(model, options.reasoning)
            if clamped != "off":
                opt["reasoning_effort"] = clamped

    opt["api_key"] = api_key

    return stream_openai_responses(model, context, opt)


def _create_client(
    model: ModelInfo,
    context: Context,
    api_key: str = "",
    options_headers: dict[str, str] | None = None,
    session_id: str | None = None,
) -> httpx.AsyncClient:
    if not api_key:
        env_key = os.environ.get("OPENAI_API_KEY")
        if not env_key:
            raise RuntimeError(
                "OpenAI API key is required. Set OPENAI_API_KEY environment variable or pass it as an argument."
            )
        api_key = env_key

    compat = get_compat(model)
    headers: dict[str, str] = {}
    if model.headers:
        headers.update(model.headers)

    if model.provider == "github-copilot":
        has_images = has_copilot_vision_input(context.messages)
        copilot_headers = build_copilot_dynamic_headers(context.messages, has_images)
        headers.update(copilot_headers)

    if session_id:
        if compat.send_session_id_header:
            headers["session_id"] = session_id
        headers["x-client-request-id"] = session_id

    if options_headers:
        headers.update(options_headers)

    if model.provider == "cloudflare-ai-gateway":
        headers["Authorization"] = headers.get("Authorization", "")
        headers["cf-aig-authorization"] = f"Bearer {api_key}"
        auth_header = None
    else:
        auth_header = f"Bearer {api_key}"

    client_headers: dict[str, str] = {}
    client_headers.update(headers)
    if auth_header is not None:
        client_headers["Authorization"] = auth_header

    base_url = model.base_url

    return httpx.AsyncClient(
        base_url=base_url,
        headers=client_headers,
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
    )


def _build_params(
    model: ModelInfo,
    context: Context,
    options: OpenAIResponsesOptions | None = None,
) -> dict[str, Any]:
    messages = convert_responses_messages(model, context, OPENAI_TOOL_CALL_PROVIDERS)

    cache_retention = resolve_cache_retention(
        options.get("cache_retention") if options else None
    )
    compat = get_compat(model)

    params: dict[str, Any] = {
        "model": model.id,
        "input": messages,
        "stream": True,
        "store": False,
    }

    prompt_cache_key = (
        None
        if cache_retention == "none"
        else (options.get("session_id") if options else None)
    )
    if prompt_cache_key is not None:
        params["prompt_cache_key"] = prompt_cache_key

    prompt_cache_retention = get_prompt_cache_retention(compat, cache_retention)
    if prompt_cache_retention is not None:
        params["prompt_cache_retention"] = prompt_cache_retention

    if options:
        max_tokens = options.get("max_tokens")
        if max_tokens:
            params["max_output_tokens"] = max_tokens
        temperature = options.get("temperature")
        if temperature is not None:
            params["temperature"] = temperature
        service_tier = options.get("service_tier")
        if service_tier is not None:
            params["service_tier"] = service_tier

    if context.tools:
        params["tools"] = convert_responses_tools(context.tools)

    if model.reasoning:
        reasoning_effort = options.get("reasoning_effort") if options else None
        reasoning_summary = options.get("reasoning_summary") if options else None
        if reasoning_effort or reasoning_summary:
            effort = (
                model.thinking_level_map.get(reasoning_effort, reasoning_effort)
                if reasoning_effort and model.thinking_level_map
                else (reasoning_effort or "medium")
            )
            params["reasoning"] = {
                "effort": effort,
                "summary": reasoning_summary or "auto",
            }
            params["include"] = ["reasoning.encrypted_content"]
        elif model.provider != "github-copilot" and (
            model.thinking_level_map is None
            or model.thinking_level_map.get("off") is not None
        ):
            effort = (
                model.thinking_level_map.get("off")
                if model.thinking_level_map
                else "none"
            )
            params["reasoning"] = {"effort": effort}

    return params


def _get_service_tier_cost_multiplier(
    model: ModelInfo,
    service_tier: str | None,
) -> float:
    if service_tier == "flex":
        return 0.5
    if service_tier == "priority":
        return 2.5 if model.id == "gpt-5.5" else 2
    return 1.0


def _apply_service_tier_pricing(
    usage: Usage,
    service_tier: str | None,
    model: ModelInfo,
) -> None:
    multiplier = _get_service_tier_cost_multiplier(model, service_tier)
    if multiplier == 1.0:
        return

    usage.cost.input *= multiplier
    usage.cost.output *= multiplier
    usage.cost.cache_read *= multiplier
    usage.cost.cache_write *= multiplier
    usage.cost.total = (
        usage.cost.input
        + usage.cost.output
        + usage.cost.cache_read
        + usage.cost.cache_write
    )


def register() -> None:
    register_api_provider(
        ApiProvider(
            api="openai-responses",
            stream=stream_openai_responses,
            stream_simple=stream_simple_openai_responses,
        )
    )
