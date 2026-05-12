"""
OpenAI Completions API (Chat Completions) provider. Implements the standard
/completions or /chat/completions streaming endpoint with reasoning effort,
tool calls, and function calling support.

Owns: stream_openai_completions(), stream_simple_openai_completions().
Delegates to: httpx for SSE streaming.

Data flow: ModelInfo + Context + Options -> OpenAI Chat Completions API ->
  AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.event_stream, httpx
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Awaitable, Callable, Literal

import httpx

from ..event_stream import AssistantMessageEventStream
from ..models import calculate_cost, clamp_thinking_level
from ..types import (
    AssistantMessage,
    Context,
    ImageContent,
    Message,
    ModelInfo,
    OpenAICompletionsCompat,
    ProviderResponse,
    SimpleStreamOptions,
    StopReason,
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
from ..providers.github_copilot_headers import (
    build_copilot_dynamic_headers,
    has_copilot_vision_input,
)

NON_VISION_USER_IMAGE_PLACEHOLDER = "(image omitted: model does not support images)"
NON_VISION_TOOL_IMAGE_PLACEHOLDER = (
    "(tool image omitted: model does not support images)"
)


class OpenAICompletionsOptions(StreamOptions):
    tool_choice: Literal["auto", "none", "required"] | dict | None = None
    reasoning_effort: ThinkingLevel | None = None
    on_payload: Callable[[dict, ModelInfo], Awaitable[dict | None]] | None = None
    on_response: Callable[[ProviderResponse, ModelInfo], Awaitable[None]] | None = None


_API_KEY_ENV_MAP: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "azure-openai-responses": "AZURE_OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "vercel-ai-gateway": "AI_GATEWAY_API_KEY",
    "zai": "ZAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "moonshotai": "MOONSHOT_API_KEY",
    "moonshotai-cn": "MOONSHOT_API_KEY",
    "huggingface": "HF_TOKEN",
    "fireworks": "FIREWORKS_API_KEY",
    "opencode": "OPENCODE_API_KEY",
    "opencode-go": "OPENCODE_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "cloudflare-workers-ai": "CLOUDFLARE_API_KEY",
    "cloudflare-ai-gateway": "CLOUDFLARE_API_KEY",
    "xiaomi": "XIAOMI_API_KEY",
    "xiaomi-token-plan-cn": "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "xiaomi-token-plan-ams": "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
    "xiaomi-token-plan-sgp": "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
}

_REASONING_FIELDS = ["reasoning_content", "reasoning", "reasoning_text"]


def sanitize_surrogates(text: str) -> str:
    return re.sub(
        r"[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]",
        "",
        text,
    )


_VALID_JSON_ESCAPES = {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}


def _is_control_character(char: str) -> bool:
    return 0x00 <= ord(char) <= 0x1F


def _escape_control_character(char: str) -> str:
    switch = {
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    if char in switch:
        return switch[char]
    return f"\\u{ord(char):04x}"


def repair_json(text: str) -> str:
    repaired = []
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
            if next_char in _VALID_JSON_ESCAPES:
                repaired.append(f"\\{next_char}")
                i += 2
                continue
            repaired.append("\\\\")
            i += 1
            continue

        if _is_control_character(char):
            repaired.append(_escape_control_character(char))
        else:
            repaired.append(char)
        i += 1

    return "".join(repaired)


def parse_json_with_repair(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = repair_json(text)
        if repaired != text:
            return json.loads(repaired)
        raise


def parse_streaming_json(text: str) -> dict[str, Any]:
    if not text or text.strip() == "":
        return {}
    try:
        result = parse_json_with_repair(text)
        if isinstance(result, dict):
            return result
        return {}
    except (json.JSONDecodeError, ValueError):
        return {}


def get_env_api_key(provider: str) -> str | None:
    if provider == "github-copilot":
        for var in ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]:
            val = os.environ.get(var)
            if val:
                return val
        return None
    if provider == "anthropic":
        val = os.environ.get("ANTHROPIC_OAUTH_TOKEN")
        if val:
            return val
        return os.environ.get("ANTHROPIC_API_KEY")
    env_var = _API_KEY_ENV_MAP.get(provider)
    if env_var:
        return os.environ.get(env_var)
    return None


def is_cloudflare_provider(provider: str) -> bool:
    return provider in ("cloudflare-workers-ai", "cloudflare-ai-gateway")


def resolve_cloudflare_base_url(model: ModelInfo) -> str:
    url = model.base_url
    if "{" not in url:
        return url

    def _replace(m: re.Match) -> str:
        name = m.group(1)
        value = os.environ.get(name)
        if not value:
            raise ValueError(
                f"{name} is required for provider {model.provider} but is not set."
            )
        return value

    return re.sub(r"\{([A-Z_][A-Z0-9_]*)\}", _replace, url)


def has_tool_history(messages: list[Message]) -> bool:
    for msg in messages:
        if msg.role == "toolResult":
            return True
        if msg.role == "assistant":
            if any(block.type == "toolCall" for block in msg.content):
                return True
    return False


def is_text_content_block(block: Any) -> bool:
    return block.type == "text"


def is_thinking_content_block(block: Any) -> bool:
    return block.type == "thinking"


def is_tool_call_block(block: Any) -> bool:
    return block.type == "toolCall"


def is_image_content_block(block: Any) -> bool:
    return block.type == "image"


def resolve_cache_retention(cache_retention: str | None) -> str:
    if cache_retention:
        return cache_retention
    if os.environ.get("PI_CACHE_RETENTION") == "long":
        return "long"
    return "short"


def _replace_images_with_placeholder(
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


def _transform_messages(
    messages: list[Message],
    model: ModelInfo,
    normalize_tool_call_id: Callable[[str], str] | None = None,
) -> list[Message]:
    if "image" not in model.input:
        transformed: list[Message] = []
        for msg in messages:
            if msg.role == "user" and isinstance(msg.content, list):
                new_content = _replace_images_with_placeholder(
                    msg.content, NON_VISION_USER_IMAGE_PLACEHOLDER
                )
                msg.content = new_content
            elif msg.role == "toolResult":
                new_content = _replace_images_with_placeholder(
                    msg.content, NON_VISION_TOOL_IMAGE_PLACEHOLDER
                )
                msg.content = new_content
            transformed.append(msg)
        messages = transformed

    tool_call_id_map: dict[str, str] = {}

    transformed_msgs: list[Message] = []
    for msg in messages:
        if msg.role == "user":
            transformed_msgs.append(msg)
        elif msg.role == "toolResult":
            normalized_id = tool_call_id_map.get(msg.tool_call_id)
            if normalized_id and normalized_id != msg.tool_call_id:
                msg.tool_call_id = normalized_id
            transformed_msgs.append(msg)
        elif msg.role == "assistant":
            assistant_msg = msg
            is_same_model = (
                assistant_msg.provider == model.provider
                and assistant_msg.api == model.api
                and assistant_msg.model == model.id
            )

            transformed_content = []
            for block in assistant_msg.content:
                if block.type == "thinking":
                    if getattr(block, "redacted", False):
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
                    transformed_content.append(block)
                elif block.type == "toolCall":
                    tc = block
                    if not is_same_model and tc.thought_signature:
                        tc = tc.model_copy(update={"thought_signature": None})
                    if not is_same_model and normalize_tool_call_id:
                        normalized_id = normalize_tool_call_id(tc.id)
                        if normalized_id != tc.id:
                            tool_call_id_map[tc.id] = normalized_id
                            tc = tc.model_copy(update={"id": normalized_id})
                    transformed_content.append(tc)
                else:
                    transformed_content.append(block)

            assistant_msg.content = transformed_content
            transformed_msgs.append(assistant_msg)
        else:
            transformed_msgs.append(msg)

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
                            timestamp=int(time.time() * 1000),
                        )
                    )
            pending_tool_calls = []
            existing_tool_result_ids = set()

    for msg in transformed_msgs:
        if msg.role == "assistant":
            insert_synthetic_tool_results()
            if msg.stop_reason in ("error", "aborted"):
                continue
            tool_calls = [b for b in msg.content if b.type == "toolCall"]
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


def convert_messages(
    model: ModelInfo,
    context: Context,
    compat: OpenAICompletionsCompat,
) -> list[dict]:
    params: list[dict] = []

    def _normalize_tool_call_id(id: str) -> str:
        if "|" in id:
            call_id = id.split("|")[0]
            return re.sub(r"[^a-zA-Z0-9_-]", "_", call_id)[:40]
        if model.provider == "openai":
            return id[:40] if len(id) > 40 else id
        return id

    transformed_messages = _transform_messages(
        context.messages,
        model,
        _normalize_tool_call_id,
    )

    if context.system_prompt:
        use_developer_role = model.reasoning and bool(compat.supports_developer_role)
        role = "developer" if use_developer_role else "system"
        params.append(
            {"role": role, "content": sanitize_surrogates(context.system_prompt)}
        )

    last_role: str | None = None
    i = 0
    while i < len(transformed_messages):
        msg = transformed_messages[i]

        if (
            bool(compat.requires_assistant_after_tool_result)
            and last_role == "toolResult"
            and msg.role == "user"
        ):
            params.append(
                {
                    "role": "assistant",
                    "content": "I have processed the tool results.",
                }
            )

        if msg.role == "user":
            if isinstance(msg.content, str):
                params.append(
                    {
                        "role": "user",
                        "content": sanitize_surrogates(msg.content),
                    }
                )
            else:
                content = []
                for item in msg.content:
                    if item.type == "text":
                        content.append(
                            {
                                "type": "text",
                                "text": sanitize_surrogates(item.text),
                            }
                        )
                    else:
                        content.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{item.mime_type};base64,{item.data}",
                                },
                            }
                        )
                if not content:
                    i += 1
                    continue
                params.append(
                    {
                        "role": "user",
                        "content": content,
                    }
                )

        elif msg.role == "assistant":
            assistant_msg: dict = {
                "role": "assistant",
                "content": "" if compat.requires_assistant_after_tool_result else None,
            }

            assistant_text_parts = [
                block
                for block in msg.content
                if is_text_content_block(block) and block.text.strip()
            ]
            assistant_text = "".join(b.text for b in assistant_text_parts)

            non_empty_thinking = [
                block
                for block in msg.content
                if is_thinking_content_block(block) and block.thinking.strip()
            ]

            if non_empty_thinking:
                if compat.requires_thinking_as_text:
                    thinking_text = "\n\n".join(
                        sanitize_surrogates(b.thinking) for b in non_empty_thinking
                    )
                    text_blocks = [{"type": "text", "text": thinking_text}]
                    text_blocks.extend(
                        {"type": "text", "text": sanitize_surrogates(b.text)}
                        for b in assistant_text_parts
                    )
                    assistant_msg["content"] = text_blocks
                else:
                    if assistant_text:
                        assistant_msg["content"] = assistant_text
                    signature = non_empty_thinking[0].thinking_signature
                    if signature:
                        assistant_msg[signature] = "\n".join(
                            sanitize_surrogates(b.thinking) for b in non_empty_thinking
                        )
            elif assistant_text:
                assistant_msg["content"] = assistant_text

            tool_calls = [b for b in msg.content if is_tool_call_block(b)]
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in tool_calls
                ]
                reasoning_details = []
                for tc in tool_calls:
                    if tc.thought_signature:
                        try:
                            reasoning_details.append(json.loads(tc.thought_signature))
                        except (json.JSONDecodeError, ValueError):
                            pass
                if reasoning_details:
                    assistant_msg["reasoning_details"] = reasoning_details

            if (
                bool(compat.requires_reasoning_content_on_assistant_messages)
                and model.reasoning
            ):
                if "reasoning_content" not in assistant_msg:
                    assistant_msg["reasoning_content"] = ""

            content = assistant_msg.get("content")
            missing_content_reasoning = (
                bool(compat.requires_reasoning_content_on_assistant_messages)
                and model.reasoning
                and content is not None
            )
            has_content = (
                content is not None
                and (
                    isinstance(content, str)
                    and len(content) > 0
                    or isinstance(content, list)
                    and len(content) > 0
                )
            ) or missing_content_reasoning
            if not has_content and "tool_calls" not in assistant_msg:
                i += 1
                last_role = msg.role
                continue
            params.append(assistant_msg)

        elif msg.role == "toolResult":
            image_blocks: list[dict] = []

            while (
                i < len(transformed_messages)
                and transformed_messages[i].role == "toolResult"
            ):
                tool_msg = transformed_messages[i]
                text_result = "\n".join(
                    b.text for b in tool_msg.content if is_text_content_block(b)
                )
                has_images = any(is_image_content_block(b) for b in tool_msg.content)
                has_text = len(text_result) > 0

                tool_result_msg: dict = {
                    "role": "tool",
                    "content": sanitize_surrogates(
                        text_result if has_text else "(see attached image)"
                    ),
                    "tool_call_id": tool_msg.tool_call_id,
                }
                if bool(compat.requires_tool_result_name) and getattr(
                    tool_msg, "tool_name", None
                ):
                    tool_result_msg["name"] = tool_msg.tool_name
                params.append(tool_result_msg)

                if has_images and "image" in model.input:
                    for block in tool_msg.content:
                        if is_image_content_block(block):
                            image_blocks.append(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{block.mime_type};base64,{block.data}",
                                    },
                                }
                            )
                i += 1

            if image_blocks:
                if bool(compat.requires_assistant_after_tool_result):
                    params.append(
                        {
                            "role": "assistant",
                            "content": "I have processed the tool results.",
                        }
                    )
                params.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Attached image(s) from tool result:",
                            },
                            *image_blocks,
                        ],
                    }
                )
                last_role = "user"
            else:
                last_role = "toolResult"
            continue

        last_role = msg.role
        i += 1

    return params


def convert_tools(
    tools: list[Tool],
    compat: OpenAICompletionsCompat,
) -> list[dict]:
    result = []
    for tool in tools:
        entry: dict = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        if compat.supports_strict_mode is not False:
            entry["function"]["strict"] = False
        result.append(entry)
    return result


def detect_compat(model: ModelInfo) -> OpenAICompletionsCompat:
    provider = model.provider
    base_url = model.base_url

    is_zai = provider == "zai" or "api.z.ai" in base_url
    is_moonshot = (
        provider in ("moonshotai", "moonshotai-cn") or "api.moonshot." in base_url
    )
    is_cloudflare_workers_ai = (
        provider == "cloudflare-workers-ai" or "api.cloudflare.com" in base_url
    )
    is_cloudflare_ai_gateway = (
        provider == "cloudflare-ai-gateway" or "gateway.ai.cloudflare.com" in base_url
    )

    is_non_standard = (
        provider == "cerebras"
        or "cerebras.ai" in base_url
        or provider == "xai"
        or "api.x.ai" in base_url
        or "chutes.ai" in base_url
        or "deepseek.com" in base_url
        or is_zai
        or is_moonshot
        or provider == "opencode"
        or "opencode.ai" in base_url
        or is_cloudflare_workers_ai
        or is_cloudflare_ai_gateway
    )

    use_max_tokens = "chutes.ai" in base_url or is_moonshot or is_cloudflare_ai_gateway
    is_grok = provider == "xai" or "api.x.ai" in base_url
    is_deepseek = provider == "deepseek" or "deepseek.com" in base_url

    cache_control_format: Literal["anthropic"] | None = None
    if provider == "openrouter" and model.id.startswith("anthropic/"):
        cache_control_format = "anthropic"

    if is_deepseek:
        thinking_format: Literal[
            "openai", "openrouter", "deepseek", "zai", "qwen", "qwen-chat-template"
        ] = "deepseek"
    elif is_zai:
        thinking_format = "zai"
    elif provider == "openrouter" or "openrouter.ai" in base_url:
        thinking_format = "openrouter"
    else:
        thinking_format = "openai"

    return OpenAICompletionsCompat(
        supports_store=not is_non_standard,
        supports_developer_role=not is_non_standard,
        supports_reasoning_effort=not is_grok
        and not is_zai
        and not is_moonshot
        and not is_cloudflare_ai_gateway,
        supports_usage_in_streaming=True,
        max_tokens_field="max_tokens" if use_max_tokens else "max_completion_tokens",
        requires_tool_result_name=False,
        requires_assistant_after_tool_result=False,
        requires_thinking_as_text=False,
        requires_reasoning_content_on_assistant_messages=is_deepseek,
        thinking_format=thinking_format,
        zai_tool_stream=False,
        supports_strict_mode=not is_moonshot and not is_cloudflare_ai_gateway,
        cache_control_format=cache_control_format,
        send_session_affinity_headers=False,
        supports_long_cache_retention=not (
            is_cloudflare_workers_ai or is_cloudflare_ai_gateway
        ),
    )


def get_compat(model: ModelInfo) -> OpenAICompletionsCompat:
    detected = detect_compat(model)
    compat = model.compat
    if not isinstance(compat, OpenAICompletionsCompat):
        return detected

    return OpenAICompletionsCompat(
        supports_store=compat.supports_store
        if compat.supports_store is not None
        else detected.supports_store,
        supports_developer_role=compat.supports_developer_role
        if compat.supports_developer_role is not None
        else detected.supports_developer_role,
        supports_reasoning_effort=compat.supports_reasoning_effort
        if compat.supports_reasoning_effort is not None
        else detected.supports_reasoning_effort,
        supports_usage_in_streaming=compat.supports_usage_in_streaming
        if compat.supports_usage_in_streaming is not None
        else detected.supports_usage_in_streaming,
        max_tokens_field=compat.max_tokens_field or detected.max_tokens_field,
        requires_tool_result_name=compat.requires_tool_result_name
        if compat.requires_tool_result_name is not None
        else detected.requires_tool_result_name,
        requires_assistant_after_tool_result=compat.requires_assistant_after_tool_result
        if compat.requires_assistant_after_tool_result is not None
        else detected.requires_assistant_after_tool_result,
        requires_thinking_as_text=compat.requires_thinking_as_text
        if compat.requires_thinking_as_text is not None
        else detected.requires_thinking_as_text,
        requires_reasoning_content_on_assistant_messages=compat.requires_reasoning_content_on_assistant_messages
        if compat.requires_reasoning_content_on_assistant_messages is not None
        else detected.requires_reasoning_content_on_assistant_messages,
        thinking_format=compat.thinking_format or detected.thinking_format,
        open_router_routing=compat.open_router_routing,
        vercel_gateway_routing=compat.vercel_gateway_routing,
        zai_tool_stream=compat.zai_tool_stream
        if compat.zai_tool_stream is not None
        else detected.zai_tool_stream,
        supports_strict_mode=compat.supports_strict_mode
        if compat.supports_strict_mode is not None
        else detected.supports_strict_mode,
        cache_control_format=compat.cache_control_format
        or detected.cache_control_format,
        send_session_affinity_headers=compat.send_session_affinity_headers
        if compat.send_session_affinity_headers is not None
        else detected.send_session_affinity_headers,
        supports_long_cache_retention=compat.supports_long_cache_retention
        if compat.supports_long_cache_retention is not None
        else detected.supports_long_cache_retention,
    )


def get_compat_cache_control(
    compat: OpenAICompletionsCompat,
    cache_retention: str,
) -> dict | None:
    if compat.cache_control_format != "anthropic" or cache_retention == "none":
        return None

    ttl: str | None = None
    if cache_retention == "long" and compat.supports_long_cache_retention:
        ttl = "1h"

    result: dict = {"type": "ephemeral"}
    if ttl:
        result["ttl"] = ttl
    return result


def _add_cache_control_to_content(
    content: Any,
    cache_control: dict,
) -> bool:
    if isinstance(content, str):
        if len(content) == 0:
            return False
        return True
    if isinstance(content, list):
        for i in range(len(content) - 1, -1, -1):
            part = content[i]
            if isinstance(part, dict) and part.get("type") == "text":
                part["cache_control"] = cache_control
                return True
    return False


def _add_cache_control_to_instruction_message(
    message: dict,
    cache_control: dict,
) -> bool:
    content = message.get("content")
    if isinstance(content, str) and len(content) > 0:
        message["content"] = [
            {"type": "text", "text": content, "cache_control": cache_control}
        ]
        return True
    return _add_cache_control_to_content(content, cache_control)


def _add_cache_control_to_conversation_message(
    message: dict,
    cache_control: dict,
) -> bool:
    if message.get("role") in ("user", "assistant"):
        content = message.get("content")
        if isinstance(content, str) and len(content) > 0:
            message["content"] = [
                {"type": "text", "text": content, "cache_control": cache_control}
            ]
            return True
        return _add_cache_control_to_content(content, cache_control)
    return False


def apply_anthropic_cache_control(
    messages: list[dict],
    tools: list[dict] | None,
    cache_control: dict,
) -> None:
    for message in messages:
        if message.get("role") in ("system", "developer"):
            _add_cache_control_to_instruction_message(message, cache_control)
            break

    if tools:
        tools[-1]["cache_control"] = cache_control

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") in ("user", "assistant"):
            if _add_cache_control_to_conversation_message(msg, cache_control):
                break


def build_params(
    model: ModelInfo,
    context: Context,
    options: OpenAICompletionsOptions | None,
    compat: OpenAICompletionsCompat,
    cache_retention: str,
) -> dict:
    messages = convert_messages(model, context, compat)
    cache_control = get_compat_cache_control(compat, cache_retention)

    params: dict = {
        "model": model.id,
        "messages": messages,
        "stream": True,
    }

    is_openai = "api.openai.com" in model.base_url
    if (is_openai and cache_retention != "none") or (
        cache_retention == "long" and bool(compat.supports_long_cache_retention)
    ):
        if options and options.session_id:
            params["prompt_cache_key"] = options.session_id

    if cache_retention == "long" and bool(compat.supports_long_cache_retention):
        params["prompt_cache_retention"] = "24h"

    if compat.supports_usage_in_streaming is not False:
        params["stream_options"] = {"include_usage": True}

    if bool(compat.supports_store):
        params["store"] = False

    if options and options.max_tokens is not None:
        if compat.max_tokens_field == "max_tokens":
            params["max_tokens"] = options.max_tokens
        else:
            params["max_completion_tokens"] = options.max_tokens

    if options and options.temperature is not None:
        params["temperature"] = options.temperature

    if context.tools and len(context.tools) > 0:
        params["tools"] = convert_tools(context.tools, compat)
        if bool(compat.zai_tool_stream):
            params["tool_stream"] = True
    elif has_tool_history(context.messages):
        params["tools"] = []

    if cache_control:
        apply_anthropic_cache_control(
            params["messages"], params.get("tools"), cache_control
        )

    if options and options.tool_choice is not None:
        params["tool_choice"] = options.tool_choice

    has_reasoning = options and options.reasoning_effort is not None
    if compat.thinking_format == "zai" and model.reasoning:
        params["enable_thinking"] = has_reasoning
    elif compat.thinking_format == "qwen" and model.reasoning:
        params["enable_thinking"] = has_reasoning
    elif compat.thinking_format == "qwen-chat-template" and model.reasoning:
        params["chat_template_kwargs"] = {
            "enable_thinking": has_reasoning,
            "preserve_thinking": True,
        }
    elif compat.thinking_format == "deepseek" and model.reasoning:
        params["thinking"] = {"type": "enabled" if has_reasoning else "disabled"}
        if has_reasoning and options and options.reasoning_effort:
            level_map = model.thinking_level_map or {}
            params["reasoning_effort"] = level_map.get(
                options.reasoning_effort, options.reasoning_effort
            )
    elif compat.thinking_format == "openrouter" and model.reasoning:
        if has_reasoning and options and options.reasoning_effort:
            level_map = model.thinking_level_map or {}
            params["reasoning"] = {
                "effort": level_map.get(
                    options.reasoning_effort, options.reasoning_effort
                ),
            }
        elif (
            model.thinking_level_map and model.thinking_level_map.get("off") is not None
        ):
            params["reasoning"] = {
                "effort": model.thinking_level_map.get("off", "none")
            }
    elif (
        options
        and options.reasoning_effort
        and model.reasoning
        and bool(compat.supports_reasoning_effort)
    ):
        level_map = model.thinking_level_map or {}
        params["reasoning_effort"] = level_map.get(
            options.reasoning_effort, options.reasoning_effort
        )
    elif (
        not has_reasoning and model.reasoning and bool(compat.supports_reasoning_effort)
    ):
        level_map = model.thinking_level_map or {}
        off_value = level_map.get("off")
        if off_value is not None:
            params["reasoning_effort"] = off_value

    if "openrouter.ai" in model.base_url and compat.open_router_routing:
        routing = compat.open_router_routing
        configured: dict = {}
        if routing.only:
            configured["only"] = routing.only
        if routing.order:
            configured["order"] = routing.order
        if routing.ignore:
            configured["ignore"] = routing.ignore
        if routing.allow_fallbacks is not None:
            configured["allow_fallbacks"] = routing.allow_fallbacks
        if routing.require_parameters is not None:
            configured["require_parameters"] = routing.require_parameters
        if routing.data_collection:
            configured["data_collection"] = routing.data_collection
        if routing.zdr is not None:
            configured["zdr"] = routing.zdr
        if routing.enforce_distillable_text is not None:
            configured["enforce_distillable_text"] = routing.enforce_distillable_text
        if routing.quantizations:
            configured["quantizations"] = routing.quantizations
        if routing.sort:
            configured["sort"] = routing.sort
        if routing.max_price:
            configured["max_price"] = routing.max_price
        if routing.preferred_min_throughput is not None:
            configured["preferred_min_throughput"] = routing.preferred_min_throughput
        if routing.preferred_max_latency is not None:
            configured["preferred_max_latency"] = routing.preferred_max_latency
        if configured:
            params["provider"] = configured

    if "ai-gateway.vercel.sh" in model.base_url and compat.vercel_gateway_routing:
        routing = compat.vercel_gateway_routing
        if routing.only or routing.order:
            gateway_options: dict = {}
            if routing.only:
                gateway_options["only"] = routing.only
            if routing.order:
                gateway_options["order"] = routing.order
            params["providerOptions"] = {"gateway": gateway_options}

    return params


def build_request_headers(
    model: ModelInfo,
    context: Context,
    api_key: str,
    options_headers: dict[str, str] | None,
    session_id: str | None,
    compat: OpenAICompletionsCompat,
) -> dict[str, str]:
    headers: dict[str, str] = {}

    if model.headers:
        headers.update(model.headers)

    if model.provider == "github-copilot":
        has_images = has_copilot_vision_input(context.messages)
        copilot_headers = build_copilot_dynamic_headers(context.messages, has_images)
        headers.update(copilot_headers)

    if session_id and bool(compat.send_session_affinity_headers):
        headers["session_id"] = session_id
        headers["x-client-request-id"] = session_id
        headers["x-session-affinity"] = session_id

    if options_headers:
        headers.update(options_headers)

    if model.provider == "cloudflare-ai-gateway":
        if "Authorization" not in headers:
            headers["cf-aig-authorization"] = f"Bearer {api_key}"
    else:
        if "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {api_key}"

    return headers


def parse_chunk_usage(
    raw_usage: dict,
    model: ModelInfo,
) -> Usage:
    prompt_tokens = raw_usage.get("prompt_tokens", 0) or 0
    prompt_tokens_details = raw_usage.get("prompt_tokens_details") or {}
    reported_cached_tokens = raw_usage.get("prompt_cache_hit_tokens", 0) or 0
    cached_from_details = (prompt_tokens_details or {}).get("cached_tokens", 0) or 0
    if cached_from_details:
        reported_cached_tokens = cached_from_details

    cache_write_tokens = (prompt_tokens_details or {}).get("cache_write_tokens", 0) or 0

    cache_read_tokens = (
        max(0, reported_cached_tokens - cache_write_tokens)
        if cache_write_tokens > 0
        else reported_cached_tokens
    )

    input_tokens = max(0, prompt_tokens - cache_read_tokens - cache_write_tokens)
    output_tokens = raw_usage.get("completion_tokens", 0) or 0

    usage = Usage(
        input=input_tokens,
        output=output_tokens,
        cache_read=cache_read_tokens,
        cache_write=cache_write_tokens,
        total_tokens=input_tokens
        + output_tokens
        + cache_read_tokens
        + cache_write_tokens,
        cost=UsageCost(input=0, output=0, cache_read=0, cache_write=0, total=0),
    )
    calculate_cost(model, usage)
    return usage


def map_stop_reason(reason: str | None) -> tuple[StopReason, str | None]:
    if reason is None:
        return ("stop", None)

    mapped: dict[str, tuple[StopReason, str | None]] = {
        "stop": ("stop", None),
        "end": ("stop", None),
        "length": ("length", None),
        "function_call": ("toolUse", None),
        "tool_calls": ("toolUse", None),
        "content_filter": ("error", "Provider finish_reason: content_filter"),
        "network_error": ("error", "Provider finish_reason: network_error"),
    }

    result = mapped.get(reason)
    if result:
        return result
    return ("error", f"Provider finish_reason: {reason}")


def stream_openai_completions(
    model: ModelInfo,
    context: Context,
    options: OpenAICompletionsOptions | None = None,
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
            api_key = (
                (options.api_key if options else None)
                or get_env_api_key(model.provider)
                or ""
            )
            compat = get_compat(model)
            cache_retention = resolve_cache_retention(
                options.cache_retention if options else None
            )
            cache_session_id = None
            if cache_retention != "none" and options:
                cache_session_id = options.session_id

            headers = build_request_headers(
                model,
                context,
                api_key,
                options.headers if options else None,
                cache_session_id,
                compat,
            )

            base_url = (
                resolve_cloudflare_base_url(model)
                if is_cloudflare_provider(model.provider)
                else model.base_url
            )
            url = f"{base_url.rstrip('/')}/chat/completions"

            params = build_params(model, context, options, compat, cache_retention)

            if options and options.on_payload:
                next_params = await options.on_payload(params, model)
                if next_params is not None:
                    params = next_params

            timeout_seconds = None
            if options and options.timeout_ms is not None:
                timeout_seconds = options.timeout_ms / 1000.0

            async with httpx.AsyncClient(headers=headers) as client:
                async with client.stream(
                    "POST",
                    url,
                    json=params,
                    timeout=httpx.Timeout(timeout_seconds)
                    if timeout_seconds
                    else httpx.Timeout(120.0),
                ) as response:
                    if options and options.on_response:
                        await options.on_response(
                            ProviderResponse(
                                status=response.status_code,
                                headers=dict(response.headers),
                            ),
                            model,
                        )

                    response.raise_for_status()
                    stream.push({"type": "start", "partial": output})

                    text_block: TextContent | None = None
                    thinking_block: ThinkingContent | None = None
                    tool_call_blocks_by_index: dict[int, ToolCall] = {}
                    tool_call_blocks_by_id: dict[str, ToolCall] = {}

                    blocks = output.content

                    def get_content_index(block: Any) -> int:
                        try:
                            return blocks.index(block)
                        except ValueError:
                            return -1

                    def finish_block(block: Any) -> None:
                        content_index = get_content_index(block)
                        if content_index == -1:
                            return
                        if isinstance(block, TextContent):
                            stream.push(
                                {
                                    "type": "text_end",
                                    "contentIndex": content_index,
                                    "content": block.text,
                                    "partial": output,
                                }
                            )
                        elif isinstance(block, ThinkingContent):
                            stream.push(
                                {
                                    "type": "thinking_end",
                                    "contentIndex": content_index,
                                    "content": block.thinking,
                                    "partial": output,
                                }
                            )
                        elif isinstance(block, ToolCall):
                            block.arguments = parse_streaming_json(
                                getattr(block, "partial_args", "")
                            )
                            for scratch in ("partial_args", "stream_index"):
                                if hasattr(block, scratch):
                                    delattr(block, scratch)
                            stream.push(
                                {
                                    "type": "toolcall_end",
                                    "contentIndex": content_index,
                                    "toolCall": block,
                                    "partial": output,
                                }
                            )

                    def ensure_text_block() -> TextContent:
                        nonlocal text_block
                        if text_block is None:
                            text_block = TextContent(type="text", text="")
                            blocks.append(text_block)
                            stream.push(
                                {
                                    "type": "text_start",
                                    "contentIndex": get_content_index(text_block),
                                    "partial": output,
                                }
                            )
                        return text_block

                    def ensure_thinking_block(signature: str) -> ThinkingContent:
                        nonlocal thinking_block
                        if thinking_block is None:
                            thinking_block = ThinkingContent(
                                type="thinking",
                                thinking="",
                                thinking_signature=signature,
                            )
                            blocks.append(thinking_block)
                            stream.push(
                                {
                                    "type": "thinking_start",
                                    "contentIndex": get_content_index(thinking_block),
                                    "partial": output,
                                }
                            )
                        return thinking_block

                    def ensure_tool_call_block(tool_call_delta: dict) -> ToolCall:
                        stream_index = tool_call_delta.get("index")
                        block = None
                        if stream_index is not None:
                            block = tool_call_blocks_by_index.get(stream_index)
                        if block is None and tool_call_delta.get("id"):
                            block = tool_call_blocks_by_id.get(tool_call_delta["id"])

                        if block is None:
                            block = ToolCall(
                                type="toolCall",
                                id=tool_call_delta.get("id", ""),
                                name=(tool_call_delta.get("function") or {}).get(
                                    "name", ""
                                ),
                                arguments={},
                            )
                            block.partial_args = ""
                            block.stream_index = stream_index
                            if stream_index is not None:
                                tool_call_blocks_by_index[stream_index] = block
                            if tool_call_delta.get("id"):
                                tool_call_blocks_by_id[tool_call_delta["id"]] = block
                            blocks.append(block)
                            stream.push(
                                {
                                    "type": "toolcall_start",
                                    "contentIndex": get_content_index(block),
                                    "partial": output,
                                }
                            )

                        if (
                            stream_index is not None
                            and getattr(block, "stream_index", None) is None
                        ):
                            block.stream_index = stream_index
                            tool_call_blocks_by_index[stream_index] = block
                        if tool_call_delta.get("id") and not block.id:
                            block.id = tool_call_delta["id"]
                            tool_call_blocks_by_id[block.id] = block
                        func = tool_call_delta.get("function") or {}
                        if func.get("name") and not block.name:
                            block.name = func["name"]

                        return block

                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:].strip()
                            if data == "[DONE]":
                                break
                            if not data:
                                continue

                            chunk = json.loads(data)

                            if chunk.get("id"):
                                output.response_id = output.response_id or chunk["id"]
                            if (
                                isinstance(chunk.get("model"), str)
                                and len(chunk["model"]) > 0
                                and chunk["model"] != model.id
                            ):
                                output.response_model = (
                                    output.response_model or chunk["model"]
                                )
                            if chunk.get("usage"):
                                output.usage = parse_chunk_usage(chunk["usage"], model)

                            choices = chunk.get("choices")
                            choice = (
                                choices[0]
                                if isinstance(choices, list) and len(choices) > 0
                                else None
                            )
                            if not choice:
                                continue

                            if not chunk.get("usage") and choice.get("usage"):
                                output.usage = parse_chunk_usage(choice["usage"], model)

                            if choice.get("finish_reason"):
                                finish_reason_result = map_stop_reason(
                                    choice["finish_reason"]
                                )
                                output.stop_reason = finish_reason_result[0]
                                if finish_reason_result[1]:
                                    output.error_message = finish_reason_result[1]

                            delta = choice.get("delta")
                            if not delta:
                                continue

                            delta_content = delta.get("content")
                            if delta_content is not None and len(delta_content) > 0:
                                block = ensure_text_block()
                                block.text += delta_content
                                stream.push(
                                    {
                                        "type": "text_delta",
                                        "contentIndex": get_content_index(block),
                                        "delta": delta_content,
                                        "partial": output,
                                    }
                                )

                            found_reasoning_field: str | None = None
                            for field in _REASONING_FIELDS:
                                value = delta.get(field)
                                if isinstance(value, str) and len(value) > 0:
                                    found_reasoning_field = field
                                    break

                            if found_reasoning_field:
                                reasoning_delta = delta[found_reasoning_field]
                                if (
                                    isinstance(reasoning_delta, str)
                                    and len(reasoning_delta) > 0
                                ):
                                    block = ensure_thinking_block(found_reasoning_field)
                                    block.thinking += reasoning_delta
                                    stream.push(
                                        {
                                            "type": "thinking_delta",
                                            "contentIndex": get_content_index(block),
                                            "delta": reasoning_delta,
                                            "partial": output,
                                        }
                                    )

                            tool_calls_delta = delta.get("tool_calls")
                            if tool_calls_delta:
                                for tc in tool_calls_delta:
                                    block = ensure_tool_call_block(tc)
                                    func = tc.get("function") or {}

                                    tool_delta = ""
                                    if func.get("arguments"):
                                        tool_delta = func["arguments"]
                                        current = (
                                            getattr(block, "partial_args", "") or ""
                                        )
                                        block.partial_args = current + func["arguments"]
                                        block.arguments = parse_streaming_json(
                                            block.partial_args
                                        )

                                    stream.push(
                                        {
                                            "type": "toolcall_delta",
                                            "contentIndex": get_content_index(block),
                                            "delta": tool_delta,
                                            "partial": output,
                                        }
                                    )

                            reasoning_details = delta.get("reasoning_details")
                            if reasoning_details and isinstance(
                                reasoning_details, list
                            ):
                                for detail in reasoning_details:
                                    if (
                                        detail.get("type") == "reasoning.encrypted"
                                        and detail.get("id")
                                        and detail.get("data")
                                    ):
                                        for b in blocks:
                                            if (
                                                isinstance(b, ToolCall)
                                                and b.id == detail["id"]
                                            ):
                                                b.thought_signature = json.dumps(detail)
                                                break

                    for block in list(blocks):
                        finish_block(block)

                    if output.stop_reason == "aborted":
                        raise ValueError("Request was aborted")
                    if output.stop_reason == "error":
                        raise ValueError(
                            output.error_message
                            or "Provider returned an error stop reason"
                        )

                    stream.push(
                        {
                            "type": "done",
                            "reason": output.stop_reason,
                            "message": output,
                        }
                    )
                    stream.end()

        except httpx.HTTPStatusError as e:
            for block in output.content:
                for scratch in ("partial_args", "stream_index"):
                    if hasattr(block, scratch):
                        delattr(block, scratch)

            output.stop_reason = "error"
            body = ""
            try:
                body = e.response.text
            except Exception:
                pass
            output.error_message = f"HTTP {e.response.status_code}: {body or str(e)}"

            stream.push(
                {"type": "error", "reason": output.stop_reason, "error": output}
            )
            stream.end()

        except Exception as error:
            for block in output.content:
                for scratch in ("partial_args", "stream_index"):
                    if hasattr(block, scratch):
                        delattr(block, scratch)

            output.stop_reason = "error"
            output.error_message = str(error)

            if hasattr(error, "error") and hasattr(error.error, "metadata"):
                raw_metadata = getattr(error.error.metadata, "raw", None)
                if raw_metadata:
                    output.error_message += f"\n{raw_metadata}"

            stream.push(
                {"type": "error", "reason": output.stop_reason, "error": output}
            )
            stream.end()

    asyncio.create_task(_run())
    return stream


def stream_simple_openai_completions(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = (options.api_key if options else None) or get_env_api_key(model.provider)
    if not api_key:
        raise ValueError(f"No API key for provider: {model.provider}")

    max_tokens = options.max_tokens if options else None
    if max_tokens is None and model.max_tokens > 0:
        max_tokens = min(model.max_tokens, 32000)

    base = StreamOptions(
        temperature=options.temperature if options else None,
        max_tokens=max_tokens,
        api_key=api_key,
        cache_retention=options.cache_retention if options else None,
        session_id=options.session_id if options else None,
        headers=options.headers if options else None,
        timeout_ms=options.timeout_ms if options else None,
        max_retries=options.max_retries if options else None,
        max_retry_delay_ms=options.max_retry_delay_ms if options else None,
        metadata=options.metadata if options else None,
    )

    clamped_reasoning = None
    if options and options.reasoning:
        clamped = clamp_thinking_level(model, options.reasoning)
        clamped_reasoning = None if clamped == "off" else clamped

    return stream_openai_completions(
        model,
        context,
        OpenAICompletionsOptions(
            **base.model_dump(exclude_none=True),
            reasoning_effort=clamped_reasoning,
        ),
    )
