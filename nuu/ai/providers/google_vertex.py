"""
Google Vertex AI provider using vertexai SDK. Authenticates via Google ADC
(Application Default Credentials) and streams via the Vertex API.

Owns: stream_google_vertex(), stream_simple_google_vertex().
Delegates to: vertexai SDK, google.auth for ADC.

Data flow: ModelInfo + Context + Options -> Vertex API -> AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.event_stream, nuu.ai.providers.google_shared,
  vertexai, google.auth
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Callable, Literal

import httpx

from google.auth import default as default_credentials
from google.auth.transport.requests import Request as AuthRequest

from ..event_stream import AssistantMessageEventStream
from ..models import calculate_cost
from ..types import (
    AssistantMessage,
    Context,
    ImageContent,
    KnownApi,
    Message,
    ModelInfo,
    SimpleStreamOptions,
    StopReason,
    StreamOptions,
    TextContent,
    ThinkingBudgets,
    ThinkingContent,
    ThinkingLevel,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
)

GoogleThinkingLevel = Literal[
    "THINKING_LEVEL_UNSPECIFIED", "MINIMAL", "LOW", "MEDIUM", "HIGH"
]
ClampedThinkingLevel = Literal["minimal", "low", "medium", "high"]


class GoogleVertexOptions(StreamOptions):
    tool_choice: Literal["auto", "none", "any"] | None = None
    thinking: dict[str, Any] | None = None
    project: str | None = None
    location: str | None = None


API_VERSION = "v1"
GCP_VERTEX_CREDENTIALS_MARKER = "gcp-vertex-credentials"


def _is_thinking_part(part: dict[str, Any]) -> bool:
    return part.get("thought") is True


def _retain_thought_signature(existing: str | None, incoming: str | None) -> str | None:
    if isinstance(incoming, str) and len(incoming) > 0:
        return incoming
    return existing


def _map_stop_reason(reason: str) -> StopReason:
    mapping: dict[str, StopReason] = {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "BLOCKLIST": "error",
        "PROHIBITED_CONTENT": "error",
        "SPII": "error",
        "SAFETY": "error",
        "IMAGE_SAFETY": "error",
        "IMAGE_PROHIBITED_CONTENT": "error",
        "IMAGE_RECITATION": "error",
        "IMAGE_OTHER": "error",
        "RECITATION": "error",
        "FINISH_REASON_UNSPECIFIED": "error",
        "OTHER": "error",
        "LANGUAGE": "error",
        "MALFORMED_FUNCTION_CALL": "error",
        "UNEXPECTED_TOOL_CALL": "error",
        "NO_IMAGE": "error",
    }
    return mapping.get(reason, "error")


def _map_tool_choice(choice: str) -> str:
    mapping = {
        "auto": "AUTO",
        "none": "NONE",
        "any": "ANY",
    }
    return mapping.get(choice, "AUTO")


def _requires_tool_call_id(model_id: str) -> bool:
    return model_id.startswith("claude-") or model_id.startswith("gpt-oss-")


def _sanitize_for_open_api(schema: Any) -> Any:
    json_schema_meta = {
        "$schema",
        "$id",
        "$anchor",
        "$dynamicAnchor",
        "$vocabulary",
        "$comment",
        "$defs",
        "definitions",
    }
    if not isinstance(schema, dict):
        return schema
    return {
        k: _sanitize_for_open_api(v)
        for k, v in schema.items()
        if k not in json_schema_meta
    }


def _convert_tools(
    tools: list[Any], use_parameters: bool = False
) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    declarations = []
    for tool in tools:
        entry: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
        }
        if use_parameters:
            entry["parameters"] = _sanitize_for_open_api(tool.parameters)
        else:
            entry["parametersJsonSchema"] = tool.parameters
        declarations.append(entry)
    return [{"functionDeclarations": declarations}]


def _transform_messages(
    messages: list[Message],
    model: ModelInfo,
    normalize_tool_call_id: Callable[[str], str] | None = None,
) -> list[Message]:
    tool_call_id_map: dict[str, str] = {}

    non_vision_user_placeholder = "(image omitted: model does not support images)"
    non_vision_tool_placeholder = "(tool image omitted: model does not support images)"

    def _replace_images_with_placeholder(
        content: list[TextContent | ImageContent], placeholder: str
    ) -> list[TextContent]:
        result: list[TextContent] = []
        previous_was_placeholder = False
        for block in content:
            if block.type == "image":
                if not previous_was_placeholder:
                    result.append(TextContent(text=placeholder))
                previous_was_placeholder = True
                continue
            result.append(TextContent(text=block.text))
            previous_was_placeholder = block.text == placeholder
        return result

    def _downgrade_unsupported_images(msgs: list[Message]) -> list[Message]:
        if "image" in model.input:
            return msgs
        result: list[Message] = []
        for msg in msgs:
            if msg.role == "user" and isinstance(msg.content, list):
                msg.content = _replace_images_with_placeholder(
                    msg.content, non_vision_user_placeholder
                )
            elif msg.role == "toolResult":
                msg.content = _replace_images_with_placeholder(
                    msg.content, non_vision_tool_placeholder
                )
            result.append(msg)
        return result

    image_aware = _downgrade_unsupported_images(messages)

    transformed: list[Message] = []
    for msg in image_aware:
        if msg.role == "user":
            transformed.append(msg)
            continue

        if msg.role == "toolResult":
            normalized_id = tool_call_id_map.get(msg.tool_call_id)
            if normalized_id and normalized_id != msg.tool_call_id:
                msg.tool_call_id = normalized_id
            transformed.append(msg)
            continue

        if msg.role == "assistant":
            assistant_msg = msg
            is_same_model = (
                assistant_msg.provider == model.provider
                and assistant_msg.api == model.api
                and assistant_msg.model == model.id
            )

            new_content: list[TextContent | ThinkingContent | ToolCall] = []
            for block in assistant_msg.content:
                if block.type == "thinking":
                    if getattr(block, "redacted", None):
                        if is_same_model:
                            new_content.append(block)
                        continue
                    if is_same_model and block.thinking_signature:
                        new_content.append(block)
                        continue
                    if not block.thinking or block.thinking.strip() == "":
                        continue
                    if is_same_model:
                        new_content.append(block)
                    else:
                        new_content.append(TextContent(text=block.thinking))
                elif block.type == "text":
                    if is_same_model:
                        new_content.append(block)
                    else:
                        new_content.append(TextContent(text=block.text))
                elif block.type == "toolCall":
                    tool_call = block
                    normalized_call = tool_call
                    if not is_same_model and tool_call.thought_signature:
                        normalized_call = tool_call.model_copy(
                            update={"thought_signature": None}
                        )
                    if not is_same_model and normalize_tool_call_id:
                        new_id = normalize_tool_call_id(tool_call.id)
                        if new_id != tool_call.id:
                            tool_call_id_map[tool_call.id] = new_id
                            normalized_call = normalized_call.model_copy(
                                update={"id": new_id}
                            )
                    new_content.append(normalized_call)

            transformed.append(
                AssistantMessage(
                    role="assistant",
                    content=new_content,
                    api=assistant_msg.api,
                    provider=assistant_msg.provider,
                    model=assistant_msg.model,
                    usage=assistant_msg.usage,
                    stop_reason=assistant_msg.stop_reason,
                    timestamp=assistant_msg.timestamp,
                )
            )
            continue

        transformed.append(msg)

    result: list[Message] = []
    pending_tool_calls: list[ToolCall] = []
    existing_tool_result_ids: set[str] = set()

    def insert_synthetic_results() -> None:
        nonlocal pending_tool_calls, existing_tool_result_ids
        if pending_tool_calls:
            for tc in pending_tool_calls:
                if tc.id not in existing_tool_result_ids:
                    result.append(
                        ToolResultMessage(
                            role="toolResult",
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            content=[TextContent(text="No result provided")],
                            is_error=True,
                            timestamp=int(time.time() * 1000),
                        )
                    )
            pending_tool_calls = []
            existing_tool_result_ids = set()

    for msg in transformed:
        if msg.role == "assistant":
            insert_synthetic_results()
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
            insert_synthetic_results()
            result.append(msg)
        else:
            result.append(msg)

    insert_synthetic_results()
    return result


def _convert_messages(model: ModelInfo, context: Context) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []

    def normalize_id(id: str) -> str:
        if not _requires_tool_call_id(model.id):
            return id
        return re.sub(r"[^a-zA-Z0-9_-]", "_", id)[:64]

    transformed = _transform_messages(context.messages, model, normalize_id)

    for msg in transformed:
        if msg.role == "user":
            if isinstance(msg.content, str):
                contents.append({"role": "user", "parts": [{"text": msg.content}]})
            else:
                parts: list[dict[str, Any]] = []
                for item in msg.content:
                    if item.type == "text":
                        parts.append({"text": item.text})
                    elif item.type == "image":
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
            parts: list[dict[str, Any]] = []
            is_same = msg.provider == model.provider and msg.model == model.id

            for block in msg.content:
                if block.type == "text":
                    if not block.text or block.text.strip() == "":
                        continue
                    entry: dict[str, Any] = {"text": block.text}
                    sig = block.text_signature if is_same else None
                    if sig and re.match(r"^[A-Za-z0-9+/]+={0,2}$", sig):
                        entry["thoughtSignature"] = sig
                    parts.append(entry)

                elif block.type == "thinking":
                    if not block.thinking or block.thinking.strip() == "":
                        continue
                    if is_same:
                        entry = {"thought": True, "text": block.thinking}
                        sig = block.thinking_signature
                        if sig and re.match(r"^[A-Za-z0-9+/]+={0,2}$", sig):
                            entry["thoughtSignature"] = sig
                        parts.append(entry)
                    else:
                        parts.append({"text": block.thinking})

                elif block.type == "toolCall":
                    entry: dict[str, Any] = {
                        "functionCall": {
                            "name": block.name,
                            "args": block.arguments or {},
                        }
                    }
                    if _requires_tool_call_id(model.id):
                        entry["functionCall"]["id"] = block.id
                    sig = block.thought_signature
                    if is_same and sig and re.match(r"^[A-Za-z0-9+/]+={0,2}$", sig):
                        entry["thoughtSignature"] = sig
                    parts.append(entry)

            if not parts:
                continue
            contents.append({"role": "model", "parts": parts})

        elif msg.role == "toolResult":
            text_content = [c for c in msg.content if c.type == "text"]
            text_result = "\n".join(c.text for c in text_content)
            image_content = (
                [c for c in msg.content if c.type == "image"]
                if "image" in model.input
                else []
            )

            has_text = len(text_result) > 0
            has_images = len(image_content) > 0

            gemini_major = _get_gemini_major_version(model.id)
            supports_multimodal_fn = gemini_major is not None and gemini_major >= 3

            response_value = (
                text_result
                if has_text
                else "(see attached image)"
                if has_images
                else ""
            )

            image_parts = [
                {
                    "inlineData": {
                        "mimeType": img.mime_type,
                        "data": img.data,
                    }
                }
                for img in image_content
            ]

            include_id = _requires_tool_call_id(model.id)
            fn_response: dict[str, Any] = {
                "name": msg.tool_name,
                "response": (
                    {"error": response_value}
                    if msg.is_error
                    else {"output": response_value}
                ),
            }
            if has_images and supports_multimodal_fn:
                fn_response["parts"] = image_parts
            if include_id:
                fn_response["id"] = msg.tool_call_id

            fn_response_part: dict[str, Any] = {"functionResponse": fn_response}

            last = contents[-1] if contents else None
            if (
                last
                and last.get("role") == "user"
                and any("functionResponse" in p for p in last.get("parts", []))
            ):
                last["parts"].append(fn_response_part)
            else:
                contents.append({"role": "user", "parts": [fn_response_part]})

            if has_images and not supports_multimodal_fn:
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {"text": "Tool result image:"},
                            *image_parts,
                        ],
                    }
                )

    return contents


def _get_gemini_major_version(model_id: str) -> int | None:
    m = re.search(r"^gemini(?:-live)?-(\d+)", model_id.lower())
    if m:
        return int(m.group(1))
    return None


def _is_gemini3_pro_model(model_id: str) -> bool:
    return bool(re.search(r"gemini-3(?:\.\d+)?-pro", model_id.lower()))


def _is_gemini3_flash_model(model_id: str) -> bool:
    return bool(re.search(r"gemini-3(?:\.\d+)?-flash", model_id.lower()))


def _get_disabled_thinking_config(model: ModelInfo) -> dict[str, Any]:
    gemini_model_id = model.id
    if _is_gemini3_pro_model(gemini_model_id):
        return {"thinkingLevel": "LOW"}
    if _is_gemini3_flash_model(gemini_model_id):
        return {"thinkingLevel": "MINIMAL"}
    return {"thinkingBudget": 0}


def _get_gemini3_thinking_level(
    effort: ClampedThinkingLevel, model_id: str
) -> GoogleThinkingLevel:
    if _is_gemini3_pro_model(model_id):
        if effort in ("minimal", "low"):
            return "LOW"
        if effort in ("medium", "high"):
            return "HIGH"
    mapping: dict[str, GoogleThinkingLevel] = {
        "minimal": "MINIMAL",
        "low": "LOW",
        "medium": "MEDIUM",
        "high": "HIGH",
    }
    return mapping.get(effort, "HIGH")


def _get_google_budget(
    model_id: str,
    effort: ClampedThinkingLevel,
    custom_budgets: ThinkingBudgets | None = None,
) -> int:
    if custom_budgets:
        val = getattr(custom_budgets, effort, None)
        if val is not None:
            return val

    if "2.5-pro" in model_id:
        budgets: dict[str, int] = {
            "minimal": 128,
            "low": 2048,
            "medium": 8192,
            "high": 32768,
        }
        return budgets[effort]

    if "2.5-flash" in model_id:
        budgets: dict[str, int] = {
            "minimal": 128,
            "low": 2048,
            "medium": 8192,
            "high": 24576,
        }
        return budgets[effort]

    return -1


def _resolve_api_key(options: GoogleVertexOptions | None) -> str | None:
    api_key = (options.api_key or "").strip() if options else ""
    if not api_key:
        api_key = (os.environ.get("GOOGLE_CLOUD_API_KEY") or "").strip()
    if not api_key or api_key == GCP_VERTEX_CREDENTIALS_MARKER:
        return None
    if re.match(r"^<[^>]+>$", api_key):
        return None
    return api_key


def _resolve_project(options: GoogleVertexOptions | None) -> str:
    project = None
    if options and options.project:
        project = options.project
    if not project:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        project = os.environ.get("GCLOUD_PROJECT")
    if not project:
        raise ValueError(
            "Vertex AI requires a project ID. "
            "Set GOOGLE_CLOUD_PROJECT/GCLOUD_PROJECT or pass project in options."
        )
    return project


def _resolve_location(options: GoogleVertexOptions | None) -> str:
    location = None
    if options and options.location:
        location = options.location
    if not location:
        location = os.environ.get("GOOGLE_CLOUD_LOCATION")
    if not location:
        raise ValueError(
            "Vertex AI requires a location. "
            "Set GOOGLE_CLOUD_LOCATION or pass location in options."
        )
    return location


def _clamp_thinking_level(
    model: ModelInfo, level: ThinkingLevel
) -> ClampedThinkingLevel:
    if level == "xhigh":
        return "high"
    return level


def _build_params(
    model: ModelInfo,
    context: Context,
    options: GoogleVertexOptions | None = None,
) -> dict[str, Any]:
    if options is None:
        options = GoogleVertexOptions()

    contents = _convert_messages(model, context)

    generation_config: dict[str, Any] = {}
    if options.temperature is not None:
        generation_config["temperature"] = options.temperature
    if options.max_tokens is not None:
        generation_config["maxOutputTokens"] = options.max_tokens

    config: dict[str, Any] = {}
    if generation_config:
        config = {**generation_config}

    if context.system_prompt:
        config["systemInstruction"] = {"parts": [{"text": context.system_prompt}]}

    converted_tools = _convert_tools(context.tools) if context.tools else None
    if converted_tools:
        config["tools"] = converted_tools

    if context.tools and options.tool_choice:
        config["toolConfig"] = {
            "functionCallingConfig": {
                "mode": _map_tool_choice(options.tool_choice),
            }
        }

    if options.thinking and options.thinking.get("enabled") and model.reasoning:
        thinking_config: dict[str, Any] = {"includeThoughts": True}
        level = options.thinking.get("level")
        budget = options.thinking.get("budget_tokens")
        if level:
            thinking_config["thinkingLevel"] = level
        elif budget is not None:
            thinking_config["thinkingBudget"] = budget
        config["thinkingConfig"] = thinking_config
    elif (
        model.reasoning
        and options.thinking is not None
        and not options.thinking.get("enabled")
    ):
        config["thinkingConfig"] = _get_disabled_thinking_config(model)

    params: dict[str, Any] = {
        "model": model.id,
        "contents": contents,
        "config": config,
    }

    return params


def _build_url(
    model: ModelInfo,
    location: str,
    project: str,
    api_version: str = API_VERSION,
) -> str:
    base_url = (model.base_url or "").strip()
    if base_url and "{location}" not in base_url:
        base = base_url.rstrip("/")
        return (
            f"{base}/{api_version}/projects/{project}/locations/{location}"
            f"/publishers/google/models/{model.id}:streamGenerateContent"
        )
    return (
        f"https://{location}-aiplatform.googleapis.com/{api_version}"
        f"/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model.id}:streamGenerateContent"
    )


def _build_auth_headers(
    api_key: str | None, location: str, project: str
) -> dict[str, str]:
    if api_key:
        return {"x-goog-api-key": api_key}

    credentials, _ = default_credentials(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    request = AuthRequest()
    credentials.refresh(request)
    token = credentials.token
    if not token:
        raise ValueError("Failed to obtain access token from ADC")
    return {"Authorization": f"Bearer {token}"}


_tool_call_counter = 0


def stream_google_vertex(
    model: ModelInfo,
    context: Context,
    options: GoogleVertexOptions | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def _run() -> None:
        global _tool_call_counter

        output = AssistantMessage(
            role="assistant",
            content=[],
            api=KnownApi.GOOGLE_VERTEX,
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
            api_key = _resolve_api_key(options)
            project = _resolve_project(options)
            location = _resolve_location(options)

            url = _build_url(model, location, project)
            headers = _build_auth_headers(api_key, location, project)
            headers["Content-Type"] = "application/json"

            if model.headers:
                headers.update(model.headers)
            if options and options.headers:
                headers.update(options.headers)

            params = _build_params(model, context, options)

            on_payload = getattr(options, "on_payload", None) if options else None
            if on_payload:
                if asyncio.iscoroutinefunction(on_payload):
                    next_params = await on_payload(params, model)
                else:
                    next_params = on_payload(params, model)
                if next_params is not None:
                    params = next_params

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

            request_body = {
                "contents": params["contents"],
            }
            config = params.get("config", {})
            if config:
                for key, value in config.items():
                    if key != "model":
                        request_body[key] = value

            timeout = None
            if options and options.timeout_ms:
                timeout = options.timeout_ms / 1000.0

            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST", url, json=request_body, headers=headers
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        chunk_response_id = chunk.get("responseId")
                        if chunk_response_id:
                            output.response_id = output.response_id or chunk_response_id

                        candidates = chunk.get("candidates")
                        if not candidates:
                            continue
                        candidate = candidates[0]
                        if not candidate:
                            continue

                        content_obj = candidate.get("content")
                        if not content_obj:
                            continue
                        parts = content_obj.get("parts", [])
                        if not parts:
                            continue

                        for part in parts:
                            text = part.get("text")
                            if text is not None:
                                is_thinking = _is_thinking_part(part)
                                if (
                                    not current_block
                                    or (
                                        is_thinking and current_block.type != "thinking"
                                    )
                                    or (
                                        not is_thinking and current_block.type != "text"
                                    )
                                ):
                                    if current_block:
                                        if current_block.type == "text":
                                            stream.push(
                                                {
                                                    "type": "text_end",
                                                    "contentIndex": block_index(),
                                                    "content": current_block.text,
                                                    "partial": output,
                                                }
                                            )
                                        else:
                                            stream.push(
                                                {
                                                    "type": "thinking_end",
                                                    "contentIndex": block_index(),
                                                    "content": current_block.thinking,
                                                    "partial": output,
                                                }
                                            )
                                    if is_thinking:
                                        current_block = ThinkingContent(
                                            thinking="",
                                            thinking_signature=None,
                                        )
                                        output.content.append(current_block)
                                        stream.push(
                                            {
                                                "type": "thinking_start",
                                                "contentIndex": block_index(),
                                                "partial": output,
                                            }
                                        )
                                    else:
                                        current_block = TextContent(text="")
                                        output.content.append(current_block)
                                        stream.push(
                                            {
                                                "type": "text_start",
                                                "contentIndex": block_index(),
                                                "partial": output,
                                            }
                                        )

                                if current_block.type == "thinking":
                                    current_block.thinking += text
                                    current_block.thinking_signature = (
                                        _retain_thought_signature(
                                            current_block.thinking_signature,
                                            part.get("thoughtSignature"),
                                        )
                                    )
                                    stream.push(
                                        {
                                            "type": "thinking_delta",
                                            "contentIndex": block_index(),
                                            "delta": text,
                                            "partial": output,
                                        }
                                    )
                                else:
                                    current_block.text += text
                                    current_block.text_signature = (
                                        _retain_thought_signature(
                                            current_block.text_signature,
                                            part.get("thoughtSignature"),
                                        )
                                    )
                                    stream.push(
                                        {
                                            "type": "text_delta",
                                            "contentIndex": block_index(),
                                            "delta": text,
                                            "partial": output,
                                        }
                                    )

                            fn_call = part.get("functionCall")
                            if fn_call:
                                if current_block:
                                    if current_block.type == "text":
                                        stream.push(
                                            {
                                                "type": "text_end",
                                                "contentIndex": block_index(),
                                                "content": current_block.text,
                                                "partial": output,
                                            }
                                        )
                                    else:
                                        stream.push(
                                            {
                                                "type": "thinking_end",
                                                "contentIndex": block_index(),
                                                "content": current_block.thinking,
                                                "partial": output,
                                            }
                                        )
                                    current_block = None

                                provided_id = fn_call.get("id")
                                needs_new_id = not provided_id or any(
                                    b.type == "toolCall" and b.id == provided_id
                                    for b in output.content
                                )
                                if needs_new_id:
                                    _tool_call_counter += 1
                                    tool_call_id = (
                                        f"{fn_call['name']}_"
                                        f"{int(time.time() * 1000)}_"
                                        f"{_tool_call_counter}"
                                    )
                                else:
                                    tool_call_id = provided_id

                                tool_call = ToolCall(
                                    id=tool_call_id,
                                    name=fn_call.get("name") or "",
                                    arguments=fn_call.get("args") or {},
                                    thought_signature=(part.get("thoughtSignature")),
                                )

                                output.content.append(tool_call)
                                stream.push(
                                    {
                                        "type": "toolcall_start",
                                        "contentIndex": block_index(),
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

                        finish_reason = candidate.get("finishReason")
                        if finish_reason:
                            output.stop_reason = _map_stop_reason(finish_reason)
                            if any(b.type == "toolCall" for b in output.content):
                                output.stop_reason = "toolUse"

                        usage_data = chunk.get("usageMetadata")
                        if usage_data:
                            output.usage = Usage(
                                input=(
                                    usage_data.get("promptTokenCount", 0)
                                    - usage_data.get("cachedContentTokenCount", 0)
                                ),
                                output=(
                                    usage_data.get("candidatesTokenCount", 0)
                                    + usage_data.get("thoughtsTokenCount", 0)
                                ),
                                cache_read=usage_data.get("cachedContentTokenCount", 0),
                                cache_write=0,
                                total_tokens=usage_data.get("totalTokenCount", 0),
                                cost=UsageCost(
                                    input=0,
                                    output=0,
                                    cache_read=0,
                                    cache_write=0,
                                    total=0,
                                ),
                            )
                            calculate_cost(model, output.usage)

            if current_block:
                if current_block.type == "text":
                    stream.push(
                        {
                            "type": "text_end",
                            "contentIndex": block_index(),
                            "content": current_block.text,
                            "partial": output,
                        }
                    )
                else:
                    stream.push(
                        {
                            "type": "thinking_end",
                            "contentIndex": block_index(),
                            "content": current_block.thinking,
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
                b = block
                if hasattr(b, "index"):
                    b.index = None  # type: ignore[attr-defined]
            signal_aborted = bool(
                getattr(options, "signal", None)
                and getattr(options.signal, "aborted", False)
            )
            output.stop_reason = "aborted" if signal_aborted else "error"
            output.error_message = str(exc)
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


def stream_simple_google_vertex(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    base: dict[str, Any] = {
        "temperature": options.temperature if options else None,
        "max_tokens": (
            min(options.max_tokens, 32000)
            if options and options.max_tokens is not None
            else None
        ),
        "api_key": options.api_key if options else None,
        "transport": options.transport if options else None,
        "cache_retention": options.cache_retention if options else None,
        "session_id": options.session_id if options else None,
        "headers": options.headers if options else None,
        "timeout_ms": options.timeout_ms if options else None,
        "max_retries": options.max_retries if options else None,
        "max_retry_delay_ms": options.max_retry_delay_ms if options else None,
        "metadata": options.metadata if options else None,
    }
    base = {k: v for k, v in base.items() if v is not None}

    if not options or not options.reasoning:
        return stream_google_vertex(
            model,
            context,
            GoogleVertexOptions(**base, thinking={"enabled": False}),
        )

    clamped = _clamp_thinking_level(model, options.reasoning)
    effort: ClampedThinkingLevel = "high" if clamped == "off" else clamped

    if _is_gemini3_pro_model(model.id) or _is_gemini3_flash_model(model.id):
        return stream_google_vertex(
            model,
            context,
            GoogleVertexOptions(
                **base,
                thinking={
                    "enabled": True,
                    "level": _get_gemini3_thinking_level(effort, model.id),
                },
            ),
        )

    return stream_google_vertex(
        model,
        context,
        GoogleVertexOptions(
            **base,
            thinking={
                "enabled": True,
                "budget_tokens": _get_google_budget(
                    model.id, effort, options.thinking_budgets if options else None
                ),
            },
        ),
    )
