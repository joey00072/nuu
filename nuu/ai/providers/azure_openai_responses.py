"""
Azure OpenAI Responses API provider. Thin wrapper over openai_responses_shared
with Azure-specific base URL, API version, and header construction.

Owns: stream_azure_openai_responses(), stream_simple_azure_openai_responses().
Delegates to: nuu.ai.providers.openai_responses_shared for shared logic.

Data flow: ModelInfo + Context + Options -> Azure-specific headers ->
  openai_responses_shared -> AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.providers.openai_responses_shared, httpx
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any, Callable, Literal
from urllib.parse import urlparse, urlunparse

import httpx

from ..api_registry import ApiProvider, register_api_provider
from ..event_stream import AssistantMessageEventStream
from ..models import clamp_thinking_level
from ..types import (
    AssistantMessage,
    Context,
    ModelInfo,
    SimpleStreamOptions,
    StreamOptions,
    ThinkingLevel,
    Usage,
    UsageCost,
)
from .openai_responses_shared import (
    convert_responses_messages,
    convert_responses_tools,
    process_responses_stream,
)

DEFAULT_AZURE_API_VERSION = "v1"
AZURE_TOOL_CALL_PROVIDERS = frozenset(
    {
        "openai",
        "openai-codex",
        "opencode",
        "azure-openai-responses",
    }
)


class AzureOpenAIResponsesOptions(StreamOptions):
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    reasoning_summary: Literal["auto", "detailed", "concise"] | None = None
    azure_api_version: str | None = None
    azure_resource_name: str | None = None
    azure_base_url: str | None = None
    azure_deployment_name: str | None = None
    on_payload: Callable[[dict[str, Any], ModelInfo], dict[str, Any] | None] | None = (
        None
    )
    on_response: Callable[[dict[str, Any], ModelInfo], None] | None = None
    signal: Any = None


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


def _parse_deployment_name_map(value: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not value:
        return result
    for entry in value.split(","):
        trimmed = entry.strip()
        if not trimmed:
            continue
        parts = trimmed.split("=", 1)
        if len(parts) != 2:
            continue
        model_id, deployment_name = parts[0].strip(), parts[1].strip()
        if not model_id or not deployment_name:
            continue
        result[model_id] = deployment_name
    return result


def _resolve_deployment_name(
    model: ModelInfo,
    options: AzureOpenAIResponsesOptions | None = None,
) -> str:
    if options and options.azure_deployment_name:
        return options.azure_deployment_name
    mapped = _parse_deployment_name_map(
        os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME_MAP")
    ).get(model.id)
    return mapped or model.id


def _normalize_azure_base_url(base_url: str) -> str:
    trimmed = base_url.strip().rstrip("/")
    parsed = urlparse(trimmed)
    if not parsed.scheme:
        raise ValueError(f"Invalid Azure OpenAI base URL: {base_url}")

    is_azure_host = parsed.hostname is not None and (
        parsed.hostname.endswith(".openai.azure.com")
        or parsed.hostname.endswith(".cognitiveservices.azure.com")
    )
    normalized_path = parsed.path.rstrip("/")

    if is_azure_host and normalized_path in ("", "/", "/openai"):
        parsed = parsed._replace(path="/openai/v1", query="")

    return urlunparse(parsed).rstrip("/")


def _build_default_base_url(resource_name: str) -> str:
    return f"https://{resource_name}.openai.azure.com/openai/v1"


def _resolve_azure_config(
    model: ModelInfo,
    options: AzureOpenAIResponsesOptions | None = None,
) -> tuple[str, str]:
    api_version = (
        (options.azure_api_version if options else None)
        or os.environ.get("AZURE_OPENAI_API_VERSION")
        or DEFAULT_AZURE_API_VERSION
    )

    base_url_str = (
        options.azure_base_url.strip() if options and options.azure_base_url else None
    ) or (os.environ.get("AZURE_OPENAI_BASE_URL", "") or None)
    resource_name = (
        options.azure_resource_name if options else None
    ) or os.environ.get("AZURE_OPENAI_RESOURCE_NAME")

    resolved = base_url_str

    if not resolved and resource_name:
        resolved = _build_default_base_url(resource_name)

    if not resolved and model.base_url:
        resolved = model.base_url

    if not resolved:
        raise ValueError(
            "Azure OpenAI base URL is required. Set AZURE_OPENAI_BASE_URL "
            "or AZURE_OPENAI_RESOURCE_NAME, or pass azure_base_url, "
            "azure_resource_name, or model.base_url."
        )

    return _normalize_azure_base_url(resolved), api_version


def _create_client(
    model: ModelInfo,
    api_key: str,
    options: AzureOpenAIResponsesOptions | None = None,
) -> httpx.AsyncClient:
    if not api_key:
        env_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
        if not env_key:
            raise ValueError(
                "Azure OpenAI API key is required. "
                "Set AZURE_OPENAI_API_KEY environment variable or pass it as an argument."
            )
        api_key = env_key

    base_url, api_version = _resolve_azure_config(model, options)

    headers: dict[str, str] = {}
    if model.headers:
        headers.update(model.headers)
    if options and options.headers:
        headers.update(options.headers)
    headers["api-key"] = api_key

    return httpx.AsyncClient(
        base_url=base_url.rstrip("/") + "/",
        headers=headers,
        params={"api-version": api_version},
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
    )


def _build_params(
    model: ModelInfo,
    context: Context,
    options: AzureOpenAIResponsesOptions | None,
    deployment_name: str,
) -> dict[str, Any]:
    messages = convert_responses_messages(model, context, AZURE_TOOL_CALL_PROVIDERS)

    params: dict[str, Any] = {
        "model": deployment_name,
        "input": messages,
        "stream": True,
    }

    if options and options.session_id:
        params["prompt_cache_key"] = options.session_id

    if options and options.max_tokens:
        params["max_output_tokens"] = options.max_tokens

    if options and options.temperature is not None:
        params["temperature"] = options.temperature

    if context.tools:
        params["tools"] = convert_responses_tools(context.tools)

    if model.reasoning:
        if options and (
            options.reasoning_effort or options.reasoning_summary is not None
        ):
            effort = (
                model.thinking_level_map.get(
                    options.reasoning_effort, options.reasoning_effort
                )
                if options.reasoning_effort and model.thinking_level_map
                else (options.reasoning_effort or "medium")
            )
            params["reasoning"] = {
                "effort": effort,
                "summary": options.reasoning_summary or "auto",
            }
            params["include"] = ["reasoning.encrypted_content"]
        elif (
            model.thinking_level_map is not None
            and model.thinking_level_map.get("off") is not None
        ):
            params["reasoning"] = {
                "effort": model.thinking_level_map.get("off"),
            }

    return params


def stream_azure_openai_responses(
    model: ModelInfo,
    context: Context,
    options: AzureOpenAIResponsesOptions | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def _run():
        deployment_name = _resolve_deployment_name(model, options)

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
            timestamp=0,
        )

        try:
            api_key = (options.api_key if options and options.api_key else None) or ""
            client = _create_client(model, api_key, options)
            params = _build_params(model, context, options, deployment_name)

            if options and options.on_payload:
                next_params = options.on_payload(params, model)
                if next_params is not None:
                    params = next_params

            request_kwargs: dict[str, Any] = {}
            if options and options.timeout_ms is not None:
                request_kwargs["timeout"] = options.timeout_ms / 1000.0

            async with client.stream(
                "POST",
                "responses",
                json=params,
                **request_kwargs,
            ) as response:
                if options and options.on_response:
                    options.on_response(
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
                await process_responses_stream(
                    _iter_sse_events(response),
                    output,
                    stream,
                    model,
                )

            if options and options.signal and getattr(options.signal, "aborted", False):
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

            if options and options.signal and getattr(options.signal, "aborted", False):
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

    import asyncio

    asyncio.create_task(_run())
    return stream


def stream_simple_azure_openai_responses(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = (options.api_key if options else None) or os.environ.get(
        f"{model.provider.upper()}_API_KEY", ""
    )
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = StreamOptions(
        temperature=options.temperature if options else None,
        max_tokens=(
            min(options.max_tokens, 32000) if (options and options.max_tokens) else None
        ),
        api_key=api_key or (options.api_key if options else None),
        cache_retention=options.cache_retention if options else None,
        session_id=options.session_id if options else None,
        headers=options.headers if options else None,
        timeout_ms=options.timeout_ms if options else None,
        max_retries=options.max_retries if options else None,
        max_retry_delay_ms=options.max_retry_delay_ms if options else None,
    )

    clamped_reasoning: ThinkingLevel | None = None
    if options and options.reasoning:
        clamped = clamp_thinking_level(model, options.reasoning)
        clamped_reasoning = None if clamped == "off" else clamped

    return stream_azure_openai_responses(
        model,
        context,
        AzureOpenAIResponsesOptions(
            **base.model_dump(exclude_none=True),
            reasoning_effort=clamped_reasoning,
        ),
    )


def register() -> None:
    register_api_provider(
        ApiProvider(
            api="azure-openai-responses",
            stream=stream_azure_openai_responses,
            stream_simple=stream_simple_azure_openai_responses,
        )
    )
