"""
AWS Bedrock Converse Stream provider using boto3. Translates Bedrock's
converse stream events into standardized AssistantMessageEvent TypedDicts.

Owns: stream_bedrock(), stream_simple_bedrock(), message/tool conversion.
Delegates to: boto3 for AWS API calls, AWS credential chain for auth.

Data flow: ModelInfo + Context + Options -> AsyncIterator[AssistantMessageEvent]

Depends on: nuu.ai.types, nuu.ai.event_stream, boto3
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from base64 import b64decode
from typing import Any, Iterator, Literal

import boto3
from botocore.config import Config as BotoConfig

from ..api_registry import ApiProvider, register_api_provider
from ..event_stream import AssistantMessageEventStream
from ..types import (
    AssistantMessage,
    CacheRetention,
    Context,
    ImageContent,
    KnownApi,
    ModelInfo,
    SimpleStreamOptions,
    StopReason,
    StreamOptions,
    TextContent,
    ThinkingBudgets,
    ThinkingContent,
    ThinkingLevel,
    Tool,
    ToolCall,
    Usage,
    UsageCost,
)

BedrockThinkingDisplay = Literal["summarized", "omitted"]


class BedrockOptions(StreamOptions):
    region: str | None = None
    profile: str | None = None
    tool_choice: str | dict | None = None
    reasoning: ThinkingLevel | None = None
    thinking_budgets: ThinkingBudgets | None = None
    interleaved_thinking: bool | None = None
    thinking_display: BedrockThinkingDisplay | None = None
    request_metadata: dict[str, str] | None = None
    bearer_token: str | None = None


_BEDROCK_ERROR_PREFIXES: dict[str, str] = {
    "InternalServerException": "Internal server error",
    "ModelStreamErrorException": "Model stream error",
    "ValidationException": "Validation error",
    "ThrottlingException": "Throttling error",
    "ServiceUnavailableException": "Service unavailable",
}

Block = dict[str, Any]


def _sanitize_surrogates(text: str) -> str:
    return text


def _parse_streaming_json(s: str) -> dict[str, Any]:
    if not s:
        return {}
    s = s.strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for i in range(len(s), 0, -1):
        try:
            return json.loads(s[:i])
        except json.JSONDecodeError:
            continue
    return {}


def _calculate_cost(model: ModelInfo, usage: Usage) -> None:
    input_cost = (usage.input / 1_000_000) * model.cost.input
    output_cost = (usage.output / 1_000_000) * model.cost.output
    cache_read_cost = (usage.cache_read / 1_000_000) * model.cost.cache_read
    cache_write_cost = (usage.cache_write / 1_000_000) * model.cost.cache_write
    usage.cost = UsageCost(
        input=input_cost,
        output=output_cost,
        cache_read=cache_read_cost,
        cache_write=cache_write_cost,
        total=input_cost + output_cost + cache_read_cost + cache_write_cost,
    )


def _format_bedrock_error(error: Exception) -> str:
    message = str(error)
    name = type(error).__name__
    prefix = _BEDROCK_ERROR_PREFIXES.get(name, name)
    return f"{prefix}: {message}"


def _get_model_match_candidates(
    model_id: str, model_name: str | None = None
) -> list[str]:
    values = [model_id]
    if model_name:
        values.append(model_name)
    candidates: list[str] = []
    for v in values:
        lower = v.lower()
        candidates.append(lower)
        candidates.append(re.sub(r"[\s_.:]+", "-", lower))
    return candidates


def _is_anthropic_claude_model(model: ModelInfo) -> bool:
    candidates = _get_model_match_candidates(model.id, model.name)
    return any("anthropic.claude" in s or "anthropic/claude" in s for s in candidates)


def _supports_adaptive_thinking(model_id: str, model_name: str | None = None) -> bool:
    candidates = _get_model_match_candidates(model_id, model_name)
    return any(
        "opus-4-6" in s or "opus-4-7" in s or "sonnet-4-6" in s for s in candidates
    )


def _supports_native_xhigh_effort(model: ModelInfo) -> bool:
    candidates = _get_model_match_candidates(model.id, model.name)
    return any("opus-4-7" in s for s in candidates)


def _map_thinking_level_to_effort(
    model: ModelInfo,
    level: ThinkingLevel | None,
) -> Literal["low", "medium", "high", "xhigh", "max"]:
    if level == "xhigh" and _supports_native_xhigh_effort(model):
        return "xhigh"
    if level and level in model.thinking_level_map:
        mapped = model.thinking_level_map[level]
        if isinstance(mapped, str):
            return mapped
    if level == "minimal" or level == "low":
        return "low"
    if level == "medium":
        return "medium"
    if level == "high":
        return "high"
    return "high"


def _supports_prompt_caching(model: ModelInfo) -> bool:
    candidates = _get_model_match_candidates(model.id, model.name)
    has_claude_ref = any("claude" in s for s in candidates)
    if not has_claude_ref:
        if os.environ.get("AWS_BEDROCK_FORCE_CACHE") == "1":
            return True
        return False
    if any("-4-" in s for s in candidates):
        return True
    if any("claude-3-7-sonnet" in s for s in candidates):
        return True
    if any("claude-3-5-haiku" in s for s in candidates):
        return True
    return False


def _supports_thinking_signature(model: ModelInfo) -> bool:
    return _is_anthropic_claude_model(model)


def _resolve_cache_retention(cache_retention: CacheRetention | None) -> CacheRetention:
    if cache_retention:
        return cache_retention
    if os.environ.get("PI_CACHE_RETENTION") == "long":
        return "long"
    return "short"


def _get_configured_bedrock_region(options: BedrockOptions) -> str | None:
    return (
        options.region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or None
    )


def _has_configured_bedrock_profile() -> bool:
    return bool(os.environ.get("AWS_PROFILE"))


def _get_standard_bedrock_endpoint_region(base_url: str | None) -> str | None:
    if not base_url:
        return None
    try:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        hostname = parsed.hostname or ""
        match = re.match(
            r"^bedrock-runtime(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$",
            hostname.lower(),
        )
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def _should_use_explicit_bedrock_endpoint(
    base_url: str,
    configured_region: str | None,
    has_configured_profile: bool,
) -> bool:
    endpoint_region = _get_standard_bedrock_endpoint_region(base_url)
    if not endpoint_region:
        return True
    return not configured_region and not has_configured_profile


def _is_gov_cloud_bedrock_target(model: ModelInfo, options: BedrockOptions) -> bool:
    region = _get_configured_bedrock_region(options)
    if region and region.lower().startswith("us-gov-"):
        return True
    model_id = model.id.lower()
    return model_id.startswith("us-gov.") or model_id.startswith("arn:aws-us-gov:")


def _normalize_tool_call_id(id: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", id)
    if len(sanitized) > 64:
        sanitized = sanitized[:64]
    return sanitized


def _transform_messages(
    context: Context,
    model: ModelInfo,
    normalize_tool_call_id_fn=None,
) -> list[dict[str, Any]]:
    messages = context.messages
    model_supports_images = "image" in model.input

    tool_call_id_map: dict[str, str] = {}

    def _replace_images_with_placeholder(content_list, placeholder: str) -> list[dict]:
        result: list[dict] = []
        previous_was_placeholder = False
        for block in content_list:
            if isinstance(block, dict) and block.get("type") == "image":
                if not previous_was_placeholder:
                    result.append({"type": "text", "text": placeholder})
                previous_was_placeholder = True
                continue
            if isinstance(block, ImageContent):
                if not previous_was_placeholder:
                    result.append({"type": "text", "text": placeholder})
                previous_was_placeholder = True
                continue
            if isinstance(block, dict):
                result.append(block)
            elif isinstance(block, TextContent):
                result.append({"type": "text", "text": block.text})
            previous_was_placeholder = (
                isinstance(block, dict) and block.get("text") == placeholder
            )
        return result

    def _downgrade_unsupported_images(msg: dict) -> dict:
        if model_supports_images:
            return msg
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                msg = dict(msg)
                msg["content"] = _replace_images_with_placeholder(
                    content, "(image omitted: model does not support images)"
                )
        elif msg.get("role") == "toolResult":
            content = msg.get("content", [])
            if isinstance(content, list):
                msg = dict(msg)
                msg["content"] = _replace_images_with_placeholder(
                    content, "(tool image omitted: model does not support images)"
                )
        return msg

    # First pass: image downgrade + message transformation
    transformed: list[dict] = []
    for msg in messages:
        msg_dict = _message_to_dict(msg)
        msg_dict = _downgrade_unsupported_images(msg_dict)

        if msg_dict["role"] == "user":
            transformed.append(msg_dict)
        elif msg_dict["role"] == "toolResult":
            normalized_id = tool_call_id_map.get(msg_dict.get("tool_call_id", ""))
            if normalized_id:
                msg_dict = dict(msg_dict)
                msg_dict["tool_call_id"] = normalized_id
            transformed.append(msg_dict)
        elif msg_dict["role"] == "assistant":
            assistant_msg = msg if isinstance(msg, AssistantMessage) else msg_dict
            is_same_model = (
                (
                    assistant_msg.get("provider") == model.provider
                    and assistant_msg.get("api") == model.api
                    and assistant_msg.get("model") == model.id
                )
                if isinstance(assistant_msg, dict)
                else (
                    assistant_msg.provider == model.provider
                    and assistant_msg.api == model.api
                    and assistant_msg.model == model.id
                )
            )

            content = msg_dict.get("content", [])
            transformed_content: list[dict] = []
            for block in content:
                block_dict = block if isinstance(block, dict) else block.model_dump()
                block_type = block_dict.get("type")
                if block_type == "thinking":
                    redacted = block_dict.get("redacted", False) or (
                        isinstance(block, ThinkingContent) and block.redacted
                    )
                    if redacted:
                        if is_same_model:
                            transformed_content.append(block_dict)
                        continue
                    thinking_sig = block_dict.get("thinking_signature") or (
                        isinstance(block, ThinkingContent) and block.thinking_signature
                    )
                    thinking_text = (
                        block_dict.get("thinking", "")
                        or (isinstance(block, ThinkingContent) and block.thinking)
                        or ""
                    )
                    if is_same_model and thinking_sig:
                        transformed_content.append(block_dict)
                    elif not thinking_text or thinking_text.strip() == "":
                        continue
                    elif is_same_model:
                        transformed_content.append(block_dict)
                    else:
                        transformed_content.append(
                            {"type": "text", "text": thinking_text}
                        )
                elif block_type == "text":
                    block_text = block_dict.get("text", "")
                    if isinstance(block, TextContent):
                        block_text = block.text
                    if is_same_model:
                        transformed_content.append({"type": "text", "text": block_text})
                    else:
                        transformed_content.append({"type": "text", "text": block_text})
                elif block_type == "toolCall":
                    tc = block_dict
                    if not is_same_model and tc.get("thought_signature"):
                        tc = dict(tc)
                        tc.pop("thought_signature", None)
                    if not is_same_model and normalize_tool_call_id_fn:
                        original_id = tc.get("id", "")
                        if isinstance(block, ToolCall):
                            original_id = block.id
                        normalized = normalize_tool_call_id_fn(original_id)
                        if normalized != original_id:
                            tool_call_id_map[original_id] = normalized
                            tc = dict(tc)
                            tc["id"] = normalized
                    transformed_content.append(tc)
                else:
                    transformed_content.append(block_dict)

            msg_dict = dict(msg_dict)
            msg_dict["content"] = transformed_content
            transformed.append(msg_dict)
        else:
            transformed.append(msg_dict)

    # Second pass: synthetic tool results for orphaned tool calls
    result: list[dict] = []
    pending_tool_calls: list[dict] = []
    existing_tool_result_ids: set[str] = set()

    def _insert_synthetic_tool_results():
        nonlocal pending_tool_calls, existing_tool_result_ids
        if pending_tool_calls:
            for tc in pending_tool_calls:
                tc_id = tc.get("id", "")
                if tc_id not in existing_tool_result_ids:
                    result.append(
                        {
                            "role": "toolResult",
                            "tool_call_id": tc_id,
                            "tool_name": tc.get("name", ""),
                            "content": [{"type": "text", "text": "No result provided"}],
                            "is_error": True,
                            "timestamp": int(time.time() * 1000),
                        }
                    )
            pending_tool_calls = []
            existing_tool_result_ids = set()

    for msg in transformed:
        if msg["role"] == "assistant":
            _insert_synthetic_tool_results()
            stop_reason = msg.get("stop_reason") or (
                getattr(msg, "stop_reason", None) if not isinstance(msg, dict) else None
            )
            if stop_reason in ("error", "aborted"):
                continue
            tool_calls = [
                b for b in msg.get("content", []) if b.get("type") == "toolCall"
            ]
            if tool_calls:
                pending_tool_calls = tool_calls
                existing_tool_result_ids = set()
            result.append(msg)
        elif msg["role"] == "toolResult":
            tr_id = msg.get("tool_call_id", "")
            existing_tool_result_ids.add(tr_id)
            result.append(msg)
        elif msg["role"] == "user":
            _insert_synthetic_tool_results()
            result.append(msg)
        else:
            result.append(msg)

    _insert_synthetic_tool_results()
    return result


def _message_to_dict(msg) -> dict[str, Any]:
    if isinstance(msg, dict):
        return msg
    d = msg.model_dump()
    return d


def _create_image_block(mime_type: str, data: str) -> dict:
    format_map = {
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }
    fmt = format_map.get(mime_type)
    if not fmt:
        raise ValueError(f"Unknown image type: {mime_type}")
    return {"source": {"bytes": b64decode(data)}, "format": fmt}


def _build_system_prompt(
    system_prompt: str | None,
    model: ModelInfo,
    cache_retention: CacheRetention,
) -> list[dict] | None:
    if not system_prompt:
        return None
    blocks: list[dict] = [{"text": _sanitize_surrogates(system_prompt)}]
    if cache_retention != "none" and _supports_prompt_caching(model):
        cache_block: dict = {"cachePoint": {"type": "default"}}
        if cache_retention == "long":
            cache_block["cachePoint"]["ttl"] = "one_hour"
        blocks.append(cache_block)
    return blocks


def _convert_messages(
    context: Context,
    model: ModelInfo,
    cache_retention: CacheRetention,
) -> list[dict]:
    transformed = _transform_messages(context, model, _normalize_tool_call_id)
    result: list[dict] = []

    i = 0
    while i < len(transformed):
        m = transformed[i]
        role = m["role"]
        if role == "user":
            content = m.get("content", "")
            blocks: list[dict] = []
            if isinstance(content, str):
                blocks = [{"text": _sanitize_surrogates(content)}]
            elif isinstance(content, list):
                for c in content:
                    c_type = c.get("type")
                    if c_type == "text":
                        blocks.append({"text": _sanitize_surrogates(c.get("text", ""))})
                    elif c_type == "image":
                        blocks.append(
                            {
                                "image": _create_image_block(
                                    c.get("mime_type", ""), c.get("data", "")
                                )
                            }
                        )
                    else:
                        raise ValueError(f"Unknown user content type: {c_type}")
            result.append({"role": "user", "content": blocks})
        elif role == "assistant":
            content = m.get("content", [])
            if not content:
                i += 1
                continue
            content_blocks: list[dict] = []
            for c in content:
                c_type = c.get("type")
                if c_type == "text":
                    text = c.get("text", "")
                    if text.strip():
                        content_blocks.append({"text": _sanitize_surrogates(text)})
                elif c_type == "toolCall":
                    content_blocks.append(
                        {
                            "toolUse": {
                                "toolUseId": c.get("id", ""),
                                "name": c.get("name", ""),
                                "input": c.get("arguments", {}),
                            },
                        }
                    )
                elif c_type == "thinking":
                    thinking = c.get("thinking", "")
                    if not thinking.strip():
                        continue
                    if _supports_thinking_signature(model):
                        signature = c.get("thinking_signature", "")
                        if not signature or not signature.strip():
                            content_blocks.append(
                                {"text": _sanitize_surrogates(thinking)}
                            )
                        else:
                            content_blocks.append(
                                {
                                    "reasoningContent": {
                                        "reasoningText": {
                                            "text": _sanitize_surrogates(thinking),
                                            "signature": signature,
                                        },
                                    },
                                }
                            )
                    else:
                        content_blocks.append(
                            {
                                "reasoningContent": {
                                    "reasoningText": {
                                        "text": _sanitize_surrogates(thinking)
                                    },
                                },
                            }
                        )
                else:
                    raise ValueError(f"Unknown assistant content type: {c_type}")
            if not content_blocks:
                i += 1
                continue
            result.append({"role": "assistant", "content": content_blocks})
        elif role == "toolResult":
            tool_results: list[dict] = []
            tool_results.append(
                {
                    "toolResult": {
                        "toolUseId": m.get("tool_call_id", ""),
                        "content": [
                            _create_image_block(
                                c.get("mime_type", ""), c.get("data", "")
                            )
                            if c.get("type") == "image"
                            else {"text": _sanitize_surrogates(c.get("text", ""))}
                            for c in m.get("content", [])
                        ],
                        "status": "error" if m.get("is_error") else "success",
                    },
                }
            )
            j = i + 1
            while j < len(transformed) and transformed[j]["role"] == "toolResult":
                next_msg = transformed[j]
                tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": next_msg.get("tool_call_id", ""),
                            "content": [
                                _create_image_block(
                                    c.get("mime_type", ""), c.get("data", "")
                                )
                                if c.get("type") == "image"
                                else {"text": _sanitize_surrogates(c.get("text", ""))}
                                for c in next_msg.get("content", [])
                            ],
                            "status": "error"
                            if next_msg.get("is_error")
                            else "success",
                        },
                    }
                )
                j += 1
            i = j - 1
            result.append({"role": "user", "content": tool_results})
        else:
            raise ValueError(f"Unknown message role: {role}")

        i += 1

    if cache_retention != "none" and _supports_prompt_caching(model) and result:
        last_msg = result[-1]
        if last_msg["role"] == "user":
            cache_block: dict = {"cachePoint": {"type": "default"}}
            if cache_retention == "long":
                cache_block["cachePoint"]["ttl"] = "one_hour"
            last_msg.setdefault("content", []).append(cache_block)

    return result


def _convert_tool_config(
    tools: list[Tool] | None,
    tool_choice: str | dict | None,
) -> dict | None:
    if not tools:
        return None
    if tool_choice == "none":
        return None

    bedrock_tools: list[dict] = []
    for tool in tools:
        bedrock_tools.append(
            {
                "toolSpec": {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": {"json": tool.parameters},
                },
            }
        )

    bedrock_tool_choice: dict | None = None
    if tool_choice == "auto":
        bedrock_tool_choice = {"auto": {}}
    elif tool_choice == "any":
        bedrock_tool_choice = {"any": {}}
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
        bedrock_tool_choice = {"tool": {"name": tool_choice["name"]}}

    return {"tools": bedrock_tools, "toolChoice": bedrock_tool_choice}


def _map_stop_reason(reason: str | None) -> StopReason:
    if reason in ("end_turn", "stop_sequence"):
        return "stop"
    if reason in ("max_tokens", "content_filtered", "model_context_window_exceeded"):
        return "length"
    if reason == "tool_use":
        return "toolUse"
    return "error"


def _build_additional_model_request_fields(
    model: ModelInfo,
    options: BedrockOptions,
) -> dict[str, Any] | None:
    if not options.reasoning or not model.reasoning:
        return None

    if _is_anthropic_claude_model(model):
        display = options.thinking_display or "summarized"
        if _is_gov_cloud_bedrock_target(model, options):
            display = "summarized"

        if _supports_adaptive_thinking(model.id, model.name):
            result: dict[str, Any] = {
                "thinking": {"type": "adaptive", "display": display},
                "output_config": {
                    "effort": _map_thinking_level_to_effort(model, options.reasoning)
                },
            }
        else:
            default_budgets: dict[ThinkingLevel, int] = {
                "minimal": 1024,
                "low": 2048,
                "medium": 8192,
                "high": 16384,
                "xhigh": 16384,
            }
            level = "high" if options.reasoning == "xhigh" else options.reasoning
            budget = (
                options.thinking_budgets.get(level)
                if options.thinking_budgets
                else None
            )
            if budget is None:
                budget = default_budgets[options.reasoning]
            result = {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": budget,
                    "display": display,
                },
            }

        if not _supports_adaptive_thinking(model.id, model.name):
            interleaved = (
                options.interleaved_thinking
                if options.interleaved_thinking is not None
                else True
            )
            if interleaved:
                result["anthropic_beta"] = ["interleaved-thinking-2025-05-14"]

        return result

    return None


def _create_bedrock_client(model: ModelInfo, options: BedrockOptions):
    configured_region = _get_configured_bedrock_region(options)
    has_configured_profile = _has_configured_bedrock_profile()
    endpoint_region = _get_standard_bedrock_endpoint_region(model.base_url)
    use_explicit_endpoint = _should_use_explicit_bedrock_endpoint(
        model.base_url,
        configured_region,
        has_configured_profile,
    )

    session_kwargs: dict[str, Any] = {}
    if options.profile:
        session_kwargs["profile_name"] = options.profile

    bearer_token = (
        options.bearer_token or os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or None
    )
    skip_auth = os.environ.get("AWS_BEDROCK_SKIP_AUTH") == "1"

    if skip_auth:
        session_kwargs["aws_access_key_id"] = "dummy-access-key"
        session_kwargs["aws_secret_access_key"] = "dummy-secret-key"

    session = boto3.Session(**session_kwargs) if session_kwargs else boto3.Session()

    client_kwargs: dict[str, Any] = {}

    if configured_region:
        client_kwargs["region_name"] = configured_region
    elif endpoint_region and use_explicit_endpoint:
        client_kwargs["region_name"] = endpoint_region
    elif not has_configured_profile and not configured_region:
        client_kwargs["region_name"] = "us-east-1"

    if use_explicit_endpoint and model.base_url:
        client_kwargs["endpoint_url"] = model.base_url

    config_kwargs: dict[str, Any] = {}

    http_proxy = (
        os.environ.get("HTTP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("https_proxy")
    )
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")

    if http_proxy:
        config_kwargs["proxies"] = {"http": http_proxy, "https": http_proxy}
        if no_proxy:
            config_kwargs["proxies"]["no_proxy"] = no_proxy

    if options.timeout_ms:
        config_kwargs["connect_timeout"] = options.timeout_ms / 1000
        config_kwargs["read_timeout"] = options.timeout_ms / 1000

    if options.max_retries is not None:
        config_kwargs["retries"] = {"max_attempts": options.max_retries + 1}

    if config_kwargs:
        client_kwargs["config"] = BotoConfig(**config_kwargs)

    client = session.client("bedrock-runtime", **client_kwargs)

    if bearer_token and not skip_auth:

        def _add_bearer_header(request, **kwargs):
            request.headers["Authorization"] = f"Bearer {bearer_token}"

        client.meta.events.register(
            "before-send.bedrock-runtime.ConverseStream", _add_bearer_header
        )

    return client


def _build_command(
    model: ModelInfo, context: Context, options: BedrockOptions
) -> dict[str, Any]:
    cache_retention = _resolve_cache_retention(options.cache_retention)

    cmd: dict[str, Any] = {
        "modelId": model.id,
        "messages": _convert_messages(context, model, cache_retention),
        "system": _build_system_prompt(context.system_prompt, model, cache_retention),
        "inferenceConfig": {},
    }

    if options.max_tokens is not None:
        cmd["inferenceConfig"]["maxTokens"] = options.max_tokens
    if options.temperature is not None:
        cmd["inferenceConfig"]["temperature"] = options.temperature

    tool_config = _convert_tool_config(context.tools, options.tool_choice)
    if tool_config:
        cmd["toolConfig"] = tool_config

    additional = _build_additional_model_request_fields(model, options)
    if additional:
        cmd["additionalModelRequestFields"] = additional

    if options.request_metadata is not None:
        cmd["requestMetadata"] = options.request_metadata

    return cmd


async def _async_iterate(sync_iter: Iterator, loop: asyncio.AbstractEventLoop):
    _SENTINEL = object()
    iterator = iter(sync_iter)

    def _next():
        try:
            return next(iterator)
        except StopIteration:
            return _SENTINEL

    while True:
        item = await loop.run_in_executor(None, _next)
        if item is _SENTINEL:
            break
        yield item


def _handle_content_block_start(
    event: dict,
    blocks: list[Block],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    index_map: dict[int, int],
    partial_jsons: dict[int, str],
):
    content_block_index = event.get("contentBlockIndex", 0)
    start = event.get("start", {})

    if "toolUse" in start:
        tool_use = start["toolUse"]
        block: Block = {
            "type": "toolCall",
            "id": tool_use.get("toolUseId", ""),
            "name": tool_use.get("name", ""),
            "arguments": {},
            "partialJson": "",
        }
        blocks.append(block)
        idx = len(blocks) - 1
        index_map[content_block_index] = idx
        partial_jsons[idx] = ""
        stream.push(
            {
                "type": "toolcall_start",
                "contentIndex": idx,
                "partial": output,
            }
        )


def _handle_content_block_delta(
    event: dict,
    blocks: list[Block],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    index_map: dict[int, int],
    partial_jsons: dict[int, str],
):
    content_block_index = event.get("contentBlockIndex", 0)
    delta = event.get("delta", {})

    idx = index_map.get(content_block_index)
    block = blocks[idx] if idx is not None else None

    if "text" in delta:
        text = delta["text"]
        if block is None:
            new_block: Block = {"type": "text", "text": ""}
            blocks.append(new_block)
            idx = len(blocks) - 1
            index_map[content_block_index] = idx
            block = blocks[idx]
            stream.push(
                {
                    "type": "text_start",
                    "contentIndex": idx,
                    "partial": output,
                }
            )
        if block["type"] == "text":
            block["text"] += text
            stream.push(
                {
                    "type": "text_delta",
                    "contentIndex": idx,
                    "delta": text,
                    "partial": output,
                }
            )
    elif "toolUse" in delta and block is not None and block["type"] == "toolCall":
        input_delta = delta["toolUse"].get("input", "")
        partial_jsons[idx] = partial_jsons.get(idx, "") + input_delta
        block["arguments"] = _parse_streaming_json(partial_jsons[idx])
        stream.push(
            {
                "type": "toolcall_delta",
                "contentIndex": idx,
                "delta": input_delta,
                "partial": output,
            }
        )
    elif "reasoningContent" in delta:
        rc = delta["reasoningContent"]
        if block is None:
            new_block: Block = {
                "type": "thinking",
                "thinking": "",
                "thinkingSignature": "",
            }
            blocks.append(new_block)
            idx = len(blocks) - 1
            index_map[content_block_index] = idx
            block = blocks[idx]
            stream.push(
                {
                    "type": "thinking_start",
                    "contentIndex": idx,
                    "partial": output,
                }
            )
        if block["type"] == "thinking":
            rc_text = rc.get("text", "")
            if rc_text:
                block["thinking"] += rc_text
                stream.push(
                    {
                        "type": "thinking_delta",
                        "contentIndex": idx,
                        "delta": rc_text,
                        "partial": output,
                    }
                )
            rc_signature = rc.get("signature", "")
            if rc_signature:
                block["thinkingSignature"] = (
                    block.get("thinkingSignature", "") or ""
                ) + rc_signature


def _handle_content_block_stop(
    event: dict,
    blocks: list[Block],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    index_map: dict[int, int],
    partial_jsons: dict[int, str],
):
    content_block_index = event.get("contentBlockIndex", 0)
    idx = index_map.get(content_block_index)
    if idx is None:
        return
    block = blocks[idx]

    if block["type"] == "text":
        stream.push(
            {
                "type": "text_end",
                "contentIndex": idx,
                "content": block.get("text", ""),
                "partial": output,
            }
        )
    elif block["type"] == "thinking":
        stream.push(
            {
                "type": "thinking_end",
                "contentIndex": idx,
                "content": block.get("thinking", ""),
                "partial": output,
            }
        )
    elif block["type"] == "toolCall":
        block["arguments"] = _parse_streaming_json(partial_jsons.get(idx, ""))
        block.pop("partialJson", None)
        stream.push(
            {
                "type": "toolcall_end",
                "contentIndex": idx,
                "toolCall": block,
                "partial": output,
            }
        )


def _handle_metadata(
    event: dict,
    model: ModelInfo,
    output: AssistantMessage,
):
    usage_info = event.get("usage", {})
    if usage_info:
        output.usage.input = usage_info.get("inputTokens", 0)
        output.usage.output = usage_info.get("outputTokens", 0)
        output.usage.cache_read = usage_info.get("cacheReadInputTokens", 0)
        output.usage.cache_write = usage_info.get("cacheWriteInputTokens", 0)
        output.usage.total_tokens = usage_info.get(
            "totalTokens", output.usage.input + output.usage.output
        )
        _calculate_cost(model, output.usage)


def stream_bedrock(
    model: ModelInfo,
    context: Context,
    options: BedrockOptions | None = None,
) -> AssistantMessageEventStream:
    opts = BedrockOptions(**options.model_dump()) if options else BedrockOptions()

    stream = AssistantMessageEventStream()

    async def _run():
        output = AssistantMessage(
            role="assistant",
            content=[],
            api=KnownApi.BEDROCK_CONVERSE_STREAM,
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

        blocks: list[Block] = []
        index_map: dict[int, int] = {}
        partial_jsons: dict[int, str] = {}
        loop = asyncio.get_event_loop()

        try:

            def _create_and_send():
                client = _create_bedrock_client(model, opts)
                cmd = _build_command(model, context, opts)
                return client, client.converse_stream(**cmd)

            client, response = await loop.run_in_executor(None, _create_and_send)

            metadata = response.get("ResponseMetadata", {})
            request_id = metadata.get("RequestId")

            response_headers: dict[str, str] = {}
            if request_id:
                response_headers["x-amzn-requestid"] = request_id

            stream.push(
                {
                    "type": "start",
                    "partial": output,
                    "contentIndex": None,
                    "delta": None,
                }
            )

            raw_stream = response.get("stream", [])
            async for raw_event in _async_iterate(raw_stream, loop):
                if "messageStart" in raw_event:
                    pass
                elif "contentBlockStart" in raw_event:
                    _handle_content_block_start(
                        raw_event["contentBlockStart"],
                        blocks,
                        output,
                        stream,
                        index_map,
                        partial_jsons,
                    )
                elif "contentBlockDelta" in raw_event:
                    _handle_content_block_delta(
                        raw_event["contentBlockDelta"],
                        blocks,
                        output,
                        stream,
                        index_map,
                        partial_jsons,
                    )
                elif "contentBlockStop" in raw_event:
                    _handle_content_block_stop(
                        raw_event["contentBlockStop"],
                        blocks,
                        output,
                        stream,
                        index_map,
                        partial_jsons,
                    )
                elif "messageStop" in raw_event:
                    output.stop_reason = _map_stop_reason(
                        raw_event["messageStop"].get("stopReason")
                    )
                elif "metadata" in raw_event:
                    _handle_metadata(raw_event["metadata"], model, output)
                elif "internalServerException" in raw_event:
                    raise raw_event["internalServerException"]
                elif "modelStreamErrorException" in raw_event:
                    raise raw_event["modelStreamErrorException"]
                elif "validationException" in raw_event:
                    raise raw_event["validationException"]
                elif "throttlingException" in raw_event:
                    raise raw_event["throttlingException"]
                elif "serviceUnavailableException" in raw_event:
                    raise raw_event["serviceUnavailableException"]

            if output.stop_reason in ("error", "aborted"):
                raise Exception("An unknown error occurred")

            stream.push(
                {
                    "type": "done",
                    "reason": output.stop_reason,
                    "message": output,
                }
            )
            stream.end()
        except Exception as e:
            for block in blocks:
                block.pop("index", None)
                block.pop("partialJson", None)

            if isinstance(e, asyncio.CancelledError):
                output.stop_reason = "aborted"
            else:
                output.stop_reason = "error"
            output.error_message = _format_bedrock_error(e)
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


def stream_simple_bedrock(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    opts = (
        SimpleStreamOptions(**options.model_dump())
        if options
        else SimpleStreamOptions()
    )

    base: dict[str, Any] = {
        "temperature": opts.temperature,
        "max_tokens": opts.max_tokens
        or (min(model.max_tokens, 32000) if model.max_tokens > 0 else None),
        "cache_retention": opts.cache_retention,
        "headers": opts.headers,
        "timeout_ms": opts.timeout_ms,
        "max_retries": opts.max_retries,
        "max_retry_delay_ms": opts.max_retry_delay_ms,
        "metadata": opts.metadata,
    }

    base = {k: v for k, v in base.items() if v is not None}

    if not opts.reasoning:
        return stream_bedrock(
            model,
            context,
            BedrockOptions(**base, reasoning=None),
        )

    if _is_anthropic_claude_model(model):
        if _supports_adaptive_thinking(model.id, model.name):
            return stream_bedrock(
                model,
                context,
                BedrockOptions(
                    **base,
                    reasoning=opts.reasoning,
                    thinking_budgets=opts.thinking_budgets,
                ),
            )

        default_budgets: dict[str, int] = {
            "minimal": 1024,
            "low": 2048,
            "medium": 8192,
            "high": 16384,
        }
        budgets = dict(default_budgets)
        if opts.thinking_budgets:
            tb = opts.thinking_budgets
            for k in ("minimal", "low", "medium", "high"):
                v = getattr(tb, k, None)
                if v is not None:
                    budgets[k] = v

        reasoning_level = opts.reasoning
        clamped = "high" if reasoning_level == "xhigh" else reasoning_level
        thinking_budget = budgets[clamped]
        min_output_tokens = 1024
        base_max = base.get("max_tokens", 0)
        max_tokens = min(base_max + thinking_budget, model.max_tokens)
        if max_tokens <= thinking_budget:
            thinking_budget = max(0, max_tokens - min_output_tokens)

        return stream_bedrock(
            model,
            context,
            BedrockOptions(
                **base,
                max_tokens=max_tokens,
                reasoning=opts.reasoning,
                thinking_budgets=ThinkingBudgets(**{clamped: thinking_budget}),
            ),
        )

    return stream_bedrock(
        model,
        context,
        BedrockOptions(
            **base,
            reasoning=opts.reasoning,
            thinking_budgets=opts.thinking_budgets,
        ),
    )


def register_bedrock_provider():
    register_api_provider(
        ApiProvider(
            api=KnownApi.BEDROCK_CONVERSE_STREAM,
            stream=stream_bedrock,
            stream_simple=stream_simple_bedrock,
        )
    )
