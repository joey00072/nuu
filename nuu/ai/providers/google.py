"""
Google Generative AI (Gemini) provider using the google-genai SDK. Translates
Google's streaming response format into standardized AssistantMessageEvent.

Owns: stream_google(), stream_simple_google().
Delegates to: google.genai SDK for API calls.

Data flow: ModelInfo + Context + Options -> google-genai SDK ->
  AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.event_stream, google.genai
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

import httpx

from ..event_stream import AssistantMessageEventStream
from ..models import calculate_cost
from ..types import (
    AssistantMessage,
    Context,
    ModelInfo,
    SimpleStreamOptions,
    StreamOptions,
    TextContent,
    ThinkingBudgets,
    ThinkingContent,
    ToolCall,
    Usage,
    UsageCost,
)
from .google_shared import (
    convert_messages,
    convert_tools,
    is_thinking_part,
    map_stop_reason,
    map_tool_choice,
    retain_thought_signature,
    sanitize_surrogates,
)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

_tool_call_counter = 0


class GoogleOptions(StreamOptions):
    tool_choice: str | None = None
    thinking: dict[str, Any] | None = None


def _get_env_api_key(provider: str) -> str | None:
    if provider == "google":
        return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if provider == "google-vertex":
        return os.environ.get("GOOGLE_VERTEX_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY"
        )
    return os.environ.get("GOOGLE_API_KEY")


def stream_google(
    model: ModelInfo,
    context: Context,
    options: GoogleOptions | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def _run():
        global _tool_call_counter
        output = AssistantMessage(
            role="assistant",
            content=[],
            api="google-generative-ai",
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
            api_key = (
                (options.api_key if options and options.api_key else None)
                or _get_env_api_key(model.provider)
                or ""
            )
            params = _build_params(model, context, options or GoogleOptions())

            base_url = (model.base_url or GEMINI_BASE_URL).rstrip("/")
            url = f"{base_url}/models/{model.id}:streamGenerateContent?alt=sse"

            headers = {
                "Content-Type": "application/json",
            }
            if api_key:
                headers["x-goog-api-key"] = api_key
            if options and options.headers:
                headers.update(options.headers)

            stream.push(
                {
                    "type": "start",
                    "contentIndex": None,
                    "delta": None,
                    "partial": output,
                }
            )
            current_block: TextContent | ThinkingContent | None = None
            blocks = output.content

            def block_index() -> int:
                return len(blocks) - 1

            timeout_setting = (options and options.timeout_ms) or 120000

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_setting / 1000.0)
            ) as client:
                async with client.stream(
                    "POST", url, json=params, headers=headers
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:]
                        elif line.startswith("{") or line.startswith("["):
                            data_str = line
                        else:
                            continue

                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        output.response_id = output.response_id or chunk.get(
                            "responseId"
                        )

                        candidate = None
                        if "candidates" in chunk and chunk["candidates"]:
                            candidate = chunk["candidates"][0]

                        if (
                            candidate
                            and "content" in candidate
                            and "parts" in candidate["content"]
                        ):
                            for part in candidate["content"]["parts"]:
                                if "text" in part and part["text"] is not None:
                                    text_val: str = part["text"]
                                    is_thinking = is_thinking_part(part)

                                    if (
                                        current_block is None
                                        or (
                                            is_thinking
                                            and not isinstance(
                                                current_block, ThinkingContent
                                            )
                                        )
                                        or (
                                            not is_thinking
                                            and not isinstance(
                                                current_block, TextContent
                                            )
                                        )
                                    ):
                                        if current_block is not None:
                                            if isinstance(current_block, TextContent):
                                                stream.push(
                                                    {
                                                        "type": "text_end",
                                                        "contentIndex": block_index(),
                                                        "delta": None,
                                                        "partial": output,
                                                    }
                                                )
                                            else:
                                                stream.push(
                                                    {
                                                        "type": "thinking_end",
                                                        "contentIndex": block_index(),
                                                        "delta": None,
                                                        "partial": output,
                                                    }
                                                )

                                        if is_thinking:
                                            current_block = ThinkingContent(
                                                type="thinking",
                                                thinking="",
                                                thinking_signature=None,
                                            )
                                            output.content.append(current_block)
                                            stream.push(
                                                {
                                                    "type": "thinking_start",
                                                    "contentIndex": block_index(),
                                                    "delta": None,
                                                    "partial": output,
                                                }
                                            )
                                        else:
                                            current_block = TextContent(
                                                type="text", text=""
                                            )
                                            output.content.append(current_block)
                                            stream.push(
                                                {
                                                    "type": "text_start",
                                                    "contentIndex": block_index(),
                                                    "delta": None,
                                                    "partial": output,
                                                }
                                            )

                                    if isinstance(current_block, ThinkingContent):
                                        current_block.thinking += text_val
                                        current_block.thinking_signature = (
                                            retain_thought_signature(
                                                current_block.thinking_signature,
                                                part.get("thoughtSignature"),
                                            )
                                        )
                                        stream.push(
                                            {
                                                "type": "thinking_delta",
                                                "contentIndex": block_index(),
                                                "delta": text_val,
                                                "partial": output,
                                            }
                                        )
                                    else:
                                        current_block.text += text_val
                                        current_block.text_signature = (
                                            retain_thought_signature(
                                                current_block.text_signature,
                                                part.get("thoughtSignature"),
                                            )
                                        )
                                        stream.push(
                                            {
                                                "type": "text_delta",
                                                "contentIndex": block_index(),
                                                "delta": text_val,
                                                "partial": output,
                                            }
                                        )

                                if (
                                    "functionCall" in part
                                    and part["functionCall"] is not None
                                ):
                                    fc = part["functionCall"]
                                    if current_block is not None:
                                        if isinstance(current_block, TextContent):
                                            stream.push(
                                                {
                                                    "type": "text_end",
                                                    "contentIndex": block_index(),
                                                    "delta": None,
                                                    "partial": output,
                                                }
                                            )
                                        else:
                                            stream.push(
                                                {
                                                    "type": "thinking_end",
                                                    "contentIndex": block_index(),
                                                    "delta": None,
                                                    "partial": output,
                                                }
                                            )
                                        current_block = None

                                    provided_id = fc.get("id")
                                    name = fc.get("name", "")
                                    args = fc.get("args") or {}
                                    needs_new_id = not provided_id or any(
                                        isinstance(b, ToolCall) and b.id == provided_id
                                        for b in output.content
                                    )
                                    if needs_new_id:
                                        _tool_call_counter += 1
                                        tool_call_id = f"{name}_{int(time.time() * 1000)}_{_tool_call_counter}"
                                    else:
                                        tool_call_id = provided_id

                                    tool_call = ToolCall(
                                        type="toolCall",
                                        id=tool_call_id,
                                        name=name,
                                        arguments=args,
                                        thought_signature=part.get("thoughtSignature"),
                                    )

                                    output.content.append(tool_call)
                                    stream.push(
                                        {
                                            "type": "toolcall_start",
                                            "contentIndex": block_index(),
                                            "delta": None,
                                            "partial": output,
                                        }
                                    )
                                    stream.push(
                                        {
                                            "type": "toolcall_delta",
                                            "contentIndex": block_index(),
                                            "delta": json.dumps(tool_call.arguments),
                                            "partial": output,
                                        }
                                    )
                                    stream.push(
                                        {
                                            "type": "toolcall_end",
                                            "contentIndex": block_index(),
                                            "toolCall": tool_call,
                                            "partial": output,
                                        }
                                    )

                        if candidate and candidate.get("finishReason"):
                            output.stop_reason = map_stop_reason(
                                candidate["finishReason"]
                            )
                            if any(isinstance(b, ToolCall) for b in output.content):
                                output.stop_reason = "toolUse"

                        if "usageMetadata" in chunk:
                            meta = chunk["usageMetadata"]
                            output.usage = Usage(
                                input=(meta.get("promptTokenCount", 0) or 0)
                                - (meta.get("cachedContentTokenCount", 0) or 0),
                                output=(meta.get("candidatesTokenCount", 0) or 0)
                                + (meta.get("thoughtsTokenCount", 0) or 0),
                                cache_read=meta.get("cachedContentTokenCount", 0) or 0,
                                cache_write=0,
                                total_tokens=meta.get("totalTokenCount", 0) or 0,
                                cost=UsageCost(
                                    input=0,
                                    output=0,
                                    cache_read=0,
                                    cache_write=0,
                                    total=0,
                                ),
                            )
                            calculate_cost(model, output.usage)

            if current_block is not None:
                if isinstance(current_block, TextContent):
                    stream.push(
                        {
                            "type": "text_end",
                            "contentIndex": block_index(),
                            "delta": None,
                            "partial": output,
                        }
                    )
                else:
                    stream.push(
                        {
                            "type": "thinking_end",
                            "contentIndex": block_index(),
                            "delta": None,
                            "partial": output,
                        }
                    )

            if output.stop_reason in ("aborted", "error"):
                raise RuntimeError("An unknown error occurred")

            stream.push(
                {"type": "done", "reason": output.stop_reason, "message": output}
            )
            stream.end()

        except Exception as exc:
            for block in output.content:
                pass
            output.stop_reason = "error"
            output.error_message = str(exc)
            stream.push(
                {"type": "error", "reason": output.stop_reason, "error": output}
            )
            stream.end()

    asyncio.create_task(_run())
    return stream


def stream_simple_google(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = (
        options.api_key if options and options.api_key else None
    ) or _get_env_api_key(model.provider)
    if not api_key:
        raise RuntimeError(f"No API key for provider: {model.provider}")

    base = _build_base_options(model, options, api_key)

    if not options or not options.reasoning:
        return stream_google(
            model, context, GoogleOptions(**{**base, "thinking": {"enabled": False}})
        )

    from ..models import clamp_thinking_level

    clamped_reasoning = clamp_thinking_level(model, options.reasoning)
    effort = "high" if clamped_reasoning == "off" else clamped_reasoning

    if (
        _is_gemini_3_pro_model(model)
        or _is_gemini_3_flash_model(model)
        or _is_gemma_4_model(model)
    ):
        return stream_google(
            model,
            context,
            GoogleOptions(
                **{
                    **base,
                    "thinking": {
                        "enabled": True,
                        "level": _get_thinking_level(effort, model),
                    },
                }
            ),
        )

    return stream_google(
        model,
        context,
        GoogleOptions(
            **{
                **base,
                "thinking": {
                    "enabled": True,
                    "budgetTokens": _get_google_budget(
                        model, effort, options.thinking_budgets
                    ),
                },
            }
        ),
    )


def _build_base_options(
    model: ModelInfo,
    options: SimpleStreamOptions | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    base: dict[str, Any] = {}
    if options:
        if options.temperature is not None:
            base["temperature"] = options.temperature
        if options.max_tokens is not None and options.max_tokens > 0:
            base["max_tokens"] = (
                min(options.max_tokens, model.max_tokens)
                if model.max_tokens > 0
                else options.max_tokens
            )
        elif model.max_tokens > 0:
            base["max_tokens"] = min(model.max_tokens, 32000)
        if options.headers:
            base["headers"] = options.headers
        if options.timeout_ms is not None:
            base["timeout_ms"] = options.timeout_ms
    if api_key:
        base["api_key"] = api_key
    return base


def _build_params(
    model: ModelInfo,
    context: Context,
    options: GoogleOptions,
) -> dict[str, Any]:
    contents = convert_messages(model, context)

    generation_config: dict[str, Any] = {}
    if options.temperature is not None:
        generation_config["temperature"] = options.temperature
    if options.max_tokens is not None:
        generation_config["maxOutputTokens"] = options.max_tokens

    config: dict[str, Any] = {}
    if generation_config:
        config.update(generation_config)
    if context.system_prompt:
        config["systemInstruction"] = {
            "parts": [{"text": sanitize_surrogates(context.system_prompt)}]
        }
    if context.tools:
        tools = convert_tools(context.tools)
        if tools:
            config["tools"] = tools

    if context.tools and options.tool_choice:
        config["toolConfig"] = {
            "functionCallingConfig": {"mode": map_tool_choice(options.tool_choice)}
        }

    if options.thinking and options.thinking.get("enabled") and model.reasoning:
        thinking_config: dict[str, Any] = {"includeThoughts": True}
        level = options.thinking.get("level")
        budget = options.thinking.get("budgetTokens")
        if level is not None:
            thinking_config["thinkingLevel"] = level
        elif budget is not None:
            thinking_config["thinkingBudget"] = budget
        config["thinkingConfig"] = thinking_config
    elif model.reasoning and options.thinking and not options.thinking.get("enabled"):
        config["thinkingConfig"] = _get_disabled_thinking_config(model)

    params: dict[str, Any] = {
        "contents": contents,
        "config": config,
    }

    return params


def _is_gemini_3_pro_model(model: ModelInfo) -> bool:
    return bool(re.search(r"gemini-3(?:\.\d+)?-pro", model.id.lower()))


def _is_gemini_3_flash_model(model: ModelInfo) -> bool:
    return bool(re.search(r"gemini-3(?:\.\d+)?-flash", model.id.lower()))


def _is_gemma_4_model(model: ModelInfo) -> bool:
    return bool(re.search(r"gemma-?4", model.id.lower()))


def _get_disabled_thinking_config(model: ModelInfo) -> dict[str, Any]:
    if _is_gemini_3_pro_model(model):
        return {"thinkingLevel": "LOW"}
    if _is_gemini_3_flash_model(model):
        return {"thinkingLevel": "MINIMAL"}
    if _is_gemma_4_model(model):
        return {"thinkingLevel": "MINIMAL"}
    return {"thinkingBudget": 0}


def _get_thinking_level(effort: str, model: ModelInfo) -> str:
    if _is_gemini_3_pro_model(model):
        if effort in ("minimal", "low"):
            return "LOW"
        if effort in ("medium", "high"):
            return "HIGH"
    if _is_gemma_4_model(model):
        if effort in ("minimal", "low"):
            return "MINIMAL"
        if effort in ("medium", "high"):
            return "HIGH"
    mapping = {
        "minimal": "MINIMAL",
        "low": "LOW",
        "medium": "MEDIUM",
        "high": "HIGH",
    }
    return mapping.get(effort, "HIGH")


def _get_google_budget(
    model: ModelInfo,
    effort: str,
    custom_budgets: ThinkingBudgets | None = None,
) -> int:
    if custom_budgets is not None:
        budget_val = getattr(custom_budgets, effort, None)
        if budget_val is not None:
            return budget_val

    model_id_lower = model.id.lower()

    if "2.5-pro" in model_id_lower:
        budgets = {
            "minimal": 128,
            "low": 2048,
            "medium": 8192,
            "high": 32768,
        }
        return budgets.get(effort, -1)

    if "2.5-flash-lite" in model_id_lower:
        budgets = {
            "minimal": 512,
            "low": 2048,
            "medium": 8192,
            "high": 24576,
        }
        return budgets.get(effort, -1)

    if "2.5-flash" in model_id_lower:
        budgets = {
            "minimal": 128,
            "low": 2048,
            "medium": 8192,
            "high": 24576,
        }
        return budgets.get(effort, -1)

    return -1
