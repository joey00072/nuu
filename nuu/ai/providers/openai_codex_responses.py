"""
OpenAI Codex Responses API provider. Supports the Codex-specific streaming
protocol including signature-based text verification, thinking blocks, and
tool use.

Owns: stream_openai_codex_responses(), stream_simple_openai_codex_responses().
Delegates to: httpx for SSE streaming, openai_responses_shared for shared logic.

Data flow: ModelInfo + Context + Options -> OpenAI Codex API ->
  AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.event_stream, nuu.ai.providers.openai_responses_shared,
  httpx
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Literal, cast

import httpx
from websockets import ClientConnection
from websockets.asyncio.client import connect as ws_connect

from ..api_registry import ApiProvider, register_api_provider
from ..event_stream import AssistantMessageEventStream
from ..models import calculate_cost, clamp_thinking_level
from ..types import (
    AssistantMessage,
    CacheRetention,
    Context,
    ImageContent,
    KnownApi,
    Message,
    ModelInfo,
    SimpleStreamOptions,
    StopReason,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Transport,
    Usage,
    UsageCost,
    UserMessage,
)

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
MAX_RETRIES = 3
BASE_DELAY_MS = 1000
CODEX_TOOL_CALL_PROVIDERS = frozenset({"openai", "openai-codex", "opencode"})
WEBSOCKET_MESSAGE_TOO_BIG_CLOSE_CODE = 1009
OPENAI_BETA_RESPONSES_WEBSOCKETS = "responses_websockets=2026-02-06"
SESSION_WEBSOCKET_CACHE_TTL_MS = 5 * 60 * 1000

CODEX_RESPONSE_STATUSES = frozenset(
    {
        "completed",
        "incomplete",
        "failed",
        "cancelled",
        "queued",
        "in_progress",
    }
)


@dataclass
class OpenAICodexResponsesOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    cancel_event: asyncio.Event | None = None
    api_key: str | None = None
    transport: Transport | None = None
    cache_retention: CacheRetention | None = None
    session_id: str | None = None
    on_payload: Callable[[dict[str, Any], ModelInfo], dict[str, Any] | None] | None = (
        None
    )
    on_response: Callable[[dict[str, Any], ModelInfo], None] | None = None
    headers: dict[str, str] | None = None
    timeout_ms: int | None = None
    max_retries: int | None = None
    max_retry_delay_ms: int | None = None
    metadata: dict[str, Any] | None = None
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None
    ) = None
    reasoning_summary: Literal["auto", "concise", "detailed", "off", "on"] | None = None
    service_tier: str | None = None
    text_verbosity: Literal["low", "medium", "high"] | None = None


def format_thrown_value(value: object) -> str:
    if isinstance(value, Exception):
        return str(value) or type(value).__name__
    if isinstance(value, str):
        return value
    return str(value)


def _imul(a: int, b: int) -> int:
    return (a * b) & 0xFFFFFFFF


def _to_base36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while n > 0:
        result = chars[n % 36] + result
        n //= 36
    return result or "0"


def short_hash(s: str) -> str:
    h1 = 0xDEADBEEF
    h2 = 0x41C6CE57
    for ch in s:
        cv = ord(ch)
        h1 = ((h1 ^ cv) * 2654435761) & 0xFFFFFFFF
        h2 = ((h2 ^ cv) * 1597334677) & 0xFFFFFFFF
    a1 = ((h1 ^ (h1 >> 16)) * 2246822507) & 0xFFFFFFFF
    b1 = ((h2 ^ (h2 >> 13)) * 3266489909) & 0xFFFFFFFF
    h1 = (a1 ^ b1) & 0xFFFFFFFF
    a2 = ((h2 ^ (h2 >> 16)) * 2246822507) & 0xFFFFFFFF
    b2 = ((h1 ^ (h1 >> 13)) * 3266489909) & 0xFFFFFFFF
    h2 = (a2 ^ b2) & 0xFFFFFFFF
    return _to_base36(h2) + _to_base36(h1)


def headers_to_record(headers: dict[str, str] | None) -> dict[str, str]:
    if headers is None:
        return {}
    return {k: v for k, v in headers.items()}


def parse_streaming_json(partial_json: str | None) -> dict[str, Any]:
    if not partial_json or partial_json.strip() == "":
        return {}
    try:
        return json.loads(partial_json)
    except json.JSONDecodeError:
        pass
    try:
        result = _partial_json_parse(partial_json)
        if result is not None:
            return result
    except Exception:
        pass
    return {}


def _partial_json_parse(s: str) -> dict[str, Any] | None:
    stripped = s.strip()
    if not stripped:
        return None
    if stripped[0] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    key = ""
    result: dict[str, Any] = {}
    mode: Literal["key", "value"] = "key"
    i = 0
    while i < len(stripped):
        ch = stripped[i]
        if escaped:
            escaped = False
        elif ch == "\\" and in_string:
            escaped = True
        elif ch == '"' and not escaped:
            in_string = not in_string
        elif not in_string:
            if ch == "{":
                if depth > 0:
                    return None
                depth += 1
            elif ch == "}":
                break
            elif ch == ":":
                mode = "value"
            elif ch == ",":
                mode = "key"
        elif in_string and mode == "key" and ch == '"':
            key = ""
            j = i + 1
            while j < len(stripped) and stripped[j] != '"':
                if stripped[j] == "\\":
                    j += 1
                    if j < len(stripped):
                        key += stripped[j]
                else:
                    key += stripped[j]
                j += 1
            i = j
            mode = "value"
        elif in_string and mode == "value" and ch == '"':
            val = ""
            j = i + 1
            while j < len(stripped) and stripped[j] != '"':
                if stripped[j] == "\\":
                    j += 1
                    if j < len(stripped):
                        val += stripped[j]
                else:
                    val += stripped[j]
                j += 1
            if key:
                result[key] = val
                key = ""
            i = j
            mode = "key"
        i += 1
    return result if result else None


def is_retryable_error(status: int, error_text: str) -> bool:
    if status in (429, 500, 502, 503, 504):
        return True
    return bool(
        re.search(
            r"rate.?limit|overloaded|service.?unavailable|upstream.?connect|connection.?refused",
            error_text,
            re.IGNORECASE,
        )
    )


async def sleep_with_cancel(ms: int, cancel_event: asyncio.Event | None = None) -> None:
    if cancel_event and cancel_event.is_set():
        raise asyncio.CancelledError("Request was aborted")
    try:
        await asyncio.wait_for(
            asyncio.sleep(ms / 1000),
            timeout=None,
        )
    except asyncio.CancelledError:
        raise


class CodexApiError(Exception):
    def __init__(
        self,
        message: str,
        code: str | None = None,
        payload: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.payload = payload


class CodexProtocolError(Exception):
    def __init__(
        self,
        message: str,
        payload: object = None,
    ):
        super().__init__(message)
        self.payload = payload


class WebSocketCloseError(Exception):
    def __init__(
        self,
        message: str,
        code: int | None = None,
        reason: str | None = None,
        was_clean: bool | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.was_clean = was_clean


def is_codex_non_transport_error(error: Exception) -> bool:
    return isinstance(error, (CodexApiError, CodexProtocolError))


def extract_account_id(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid token")
        payload_raw = parts[1]
        padding = 4 - len(payload_raw) % 4
        if padding != 4:
            payload_raw += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_raw))
        account_id = payload.get(JWT_CLAIM_PATH, {}).get("chatgpt_account_id")
        if not account_id:
            raise ValueError("No account ID in token")
        return account_id
    except Exception as e:
        raise ValueError("Failed to extract accountId from token") from e


def create_codex_request_id() -> str:
    return str(uuid.uuid4())


def build_user_agent() -> str:
    return f"pi ({platform.system()} {platform.release()}; {platform.machine()})"


def build_base_codex_headers(
    init_headers: dict[str, str] | None,
    additional_headers: dict[str, str] | None,
    account_id: str,
    token: str,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if init_headers:
        headers.update(init_headers)
    if additional_headers:
        headers.update(additional_headers)
    headers["Authorization"] = f"Bearer {token}"
    headers["chatgpt-account-id"] = account_id
    headers["originator"] = "pi"
    headers["User-Agent"] = build_user_agent()
    return headers


def build_sse_headers(
    init_headers: dict[str, str] | None,
    additional_headers: dict[str, str] | None,
    account_id: str,
    token: str,
    session_id: str | None = None,
) -> dict[str, str]:
    headers = build_base_codex_headers(
        init_headers, additional_headers, account_id, token
    )
    headers["OpenAI-Beta"] = "responses=experimental"
    headers["accept"] = "text/event-stream"
    headers["content-type"] = "application/json"
    if session_id:
        headers["session_id"] = session_id
        headers["x-client-request-id"] = session_id
    return headers


def build_websocket_headers(
    init_headers: dict[str, str] | None,
    additional_headers: dict[str, str] | None,
    account_id: str,
    token: str,
    request_id: str,
) -> dict[str, str]:
    headers = build_base_codex_headers(
        init_headers, additional_headers, account_id, token
    )
    for key in ("accept", "content-type", "OpenAI-Beta", "openai-beta"):
        headers.pop(key, None)
    headers["OpenAI-Beta"] = OPENAI_BETA_RESPONSES_WEBSOCKETS
    headers["x-client-request-id"] = request_id
    headers["session_id"] = request_id
    return headers


def resolve_codex_url(base_url: str | None) -> str:
    raw = base_url.strip() if base_url and base_url.strip() else DEFAULT_CODEX_BASE_URL
    normalized = raw.rstrip("/")
    if normalized.endswith("/codex/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    return f"{normalized}/codex/responses"


def resolve_codex_websocket_url(base_url: str | None) -> str:
    url = resolve_codex_url(base_url)
    if url.startswith("https:"):
        url = "wss:" + url[6:]
    elif url.startswith("http:"):
        url = "ws:" + url[5:]
    else:
        url = "wss:" + url if not url.startswith("ws") else url
    return url


def normalize_codex_status(status: object) -> str | None:
    if isinstance(status, str) and status in CODEX_RESPONSE_STATUSES:
        return status
    return None


def map_stop_reason(status: str | None) -> StopReason:
    if not status:
        return "stop"
    if status == "completed":
        return "stop"
    if status == "incomplete":
        return "length"
    if status in ("failed", "cancelled"):
        return "error"
    return "stop"


def get_service_tier_cost_multiplier(model_id: str, service_tier: str | None) -> float:
    if service_tier == "flex":
        return 0.5
    if service_tier == "priority":
        return 2.5 if model_id == "gpt-5.5" else 2.0
    return 1.0


def apply_service_tier_pricing(
    usage: Usage,
    service_tier: str | None,
    model_id: str,
) -> None:
    multiplier = get_service_tier_cost_multiplier(model_id, service_tier)
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


def resolve_service_tier(
    response_service_tier: str | None,
    request_service_tier: str | None,
) -> str | None:
    if response_service_tier == "default" and request_service_tier in (
        "flex",
        "priority",
    ):
        return request_service_tier
    return response_service_tier or request_service_tier


async def parse_sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    buffer = ""
    decoder = json.JSONDecoder()
    try:
        async for chunk in response.aiter_bytes():
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                idx = buffer.index("\n\n")
                block = buffer[:idx]
                buffer = buffer[idx + 2 :]
                data_lines = []
                for line in block.split("\n"):
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data:
                            data_lines.append(data)
                if data_lines:
                    data_str = "\n".join(data_lines).strip()
                    if data_str and data_str != "[DONE]":
                        try:
                            yield decoder.decode(data_str)
                        except json.JSONDecodeError as cause:
                            raise CodexProtocolError(
                                f"Invalid Codex SSE JSON: {format_thrown_value(cause)}",
                                payload=data_str,
                            ) from cause
    finally:
        await response.aclose()


async def map_codex_events(
    events: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    async for event in events:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue
        if event_type == "error":
            code = event.get("code", "")
            message = event.get("message", "")
            raise CodexApiError(
                f"Codex error: {message or code or json.dumps(event)}",
                code=code or None,
                payload=event,
            )
        if event_type == "response.failed":
            resp = event.get("response")
            if isinstance(resp, dict):
                err = resp.get("error")
                if isinstance(err, dict):
                    code = err.get("code")
                    message = err.get("message")
                    raise CodexApiError(
                        message or "Codex response failed", code=code, payload=event
                    )
            raise CodexApiError("Codex response failed", payload=event)
        if event_type in ("response.done", "response.completed", "response.incomplete"):
            resp = event.get("response")
            if isinstance(resp, dict):
                normalized = dict(resp)
                normalized["status"] = normalize_codex_status(resp.get("status"))
                yield {"type": "response.completed", "response": normalized}
            else:
                yield {"type": "response.completed", "response": resp}
            return
        yield event


def _encode_text_signature_v1(item_id: str, phase: str | None = None) -> str:
    payload: dict[str, object] = {"v": 1, "id": item_id}
    if phase:
        payload["phase"] = phase
    return json.dumps(payload)


def _parse_text_signature(signature: str | None) -> dict[str, str] | None:
    if not signature:
        return None
    if signature.startswith("{"):
        try:
            parsed = json.loads(signature)
            if (
                isinstance(parsed, dict)
                and parsed.get("v") == 1
                and isinstance(parsed.get("id"), str)
            ):
                result: dict[str, str] = {"id": parsed["id"]}
                phase = parsed.get("phase")
                if phase in ("commentary", "final_answer"):
                    result["phase"] = phase
                return result
        except json.JSONDecodeError:
            pass
    return {"id": signature}


def _sanitize_surrogates(text: str) -> str:
    return text


def _normalize_id_part(part: str) -> str:
    sanitized = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in part)
    normalized = sanitized[:64]
    return normalized.rstrip("_")


def _build_foreign_responses_item_id(item_id: str) -> str:
    normalized = f"fc_{short_hash(item_id)}"
    return normalized[:64]


def transform_messages(
    messages: list[Message],
    model: ModelInfo,
    normalize_tool_call_id: Callable[[str, ModelInfo, AssistantMessage], str]
    | None = None,
) -> list[Message]:
    NON_VISION_USER_PLACEHOLDER = "(image omitted: model does not support images)"
    NON_VISION_TOOL_PLACEHOLDER = "(tool image omitted: model does not support images)"

    def _replace_images_with_placeholder(
        content: list[TextContent | ImageContent], placeholder: str
    ) -> list[TextContent]:
        result: list[TextContent] = []
        previous_was_placeholder = False
        for block in content:
            if block.type == "image":
                if not previous_was_placeholder:
                    result.append(TextContent(type="text", text=placeholder))
                previous_was_placeholder = True
            else:
                result.append(block)
                previous_was_placeholder = block.text == placeholder
        return result

    def _downgrade_images(msgs: list[Message]) -> list[Message]:
        if "image" in model.input:
            return msgs
        result: list[Message] = []
        for msg in msgs:
            if (
                msg.role == "user"
                and isinstance(msg, UserMessage)
                and isinstance(msg.content, list)
            ):
                result.append(
                    UserMessage(
                        role="user",
                        content=cast(
                            list[TextContent | ImageContent],
                            _replace_images_with_placeholder(
                                msg.content, NON_VISION_USER_PLACEHOLDER
                            ),
                        ),
                        timestamp=msg.timestamp,
                    )
                )
            elif msg.role == "toolResult" and isinstance(msg, ToolResultMessage):
                result.append(
                    ToolResultMessage(
                        role="toolResult",
                        tool_call_id=msg.tool_call_id,
                        tool_name=msg.tool_name,
                        content=cast(
                            list[TextContent | ImageContent],
                            _replace_images_with_placeholder(
                                msg.content, NON_VISION_TOOL_PLACEHOLDER
                            ),
                        ),
                        details=msg.details,
                        is_error=msg.is_error,
                        timestamp=msg.timestamp,
                    )
                )
            else:
                result.append(msg)
        return result

    tool_call_id_map: dict[str, str] = {}
    image_aware = _downgrade_images(messages)
    transformed: list[Message] = []
    for msg in image_aware:
        if msg.role == "user":
            transformed.append(msg)
        elif msg.role == "toolResult" and isinstance(msg, ToolResultMessage):
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
        elif msg.role == "assistant" and isinstance(msg, AssistantMessage):
            is_same_model = (
                msg.provider == model.provider
                and msg.api == model.api
                and msg.model == model.id
            )
            transformed_content: list[TextContent | ThinkingContent | ToolCall] = []
            for block in msg.content:
                if block.type == "thinking":
                    tb = block
                    if tb.redacted:
                        if is_same_model:
                            transformed_content.append(tb)
                    elif is_same_model and tb.thinking_signature:
                        transformed_content.append(tb)
                    elif not tb.thinking or tb.thinking.strip() == "":
                        pass
                    elif is_same_model:
                        transformed_content.append(tb)
                    else:
                        transformed_content.append(
                            TextContent(type="text", text=tb.thinking)
                        )
                elif block.type == "text":
                    if is_same_model:
                        transformed_content.append(block)
                    else:
                        transformed_content.append(
                            TextContent(type="text", text=block.text)
                        )
                elif block.type == "toolCall":
                    tc = block
                    normalized_tc = tc
                    if not is_same_model and tc.thought_signature:
                        normalized_tc = ToolCall(
                            type="toolCall",
                            id=tc.id,
                            name=tc.name,
                            arguments=tc.arguments,
                            thought_signature=None,
                        )
                    if not is_same_model and normalize_tool_call_id:
                        normalized_id = normalize_tool_call_id(tc.id, model, msg)
                        if normalized_id != tc.id:
                            tool_call_id_map[tc.id] = normalized_id
                            normalized_tc = ToolCall(
                                type="toolCall",
                                id=normalized_id,
                                name=normalized_tc.name,
                                arguments=normalized_tc.arguments,
                                thought_signature=normalized_tc.thought_signature,
                            )
                    transformed_content.append(normalized_tc)
            if not transformed_content:
                continue
            transformed.append(
                AssistantMessage(
                    role="assistant",
                    content=transformed_content,
                    api=msg.api,
                    provider=msg.provider,
                    model=msg.model,
                    usage=msg.usage,
                    stop_reason=msg.stop_reason,
                    error_message=msg.error_message,
                    timestamp=msg.timestamp,
                    response_id=msg.response_id,
                    response_model=msg.response_model,
                )
            )
        else:
            transformed.append(msg)

    result: list[Message] = []
    pending_tool_calls: list[ToolCall] = []
    existing_tool_result_ids: set[str] = set()

    def _insert_synthetic() -> None:
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

    for msg in transformed:
        if msg.role == "assistant" and isinstance(msg, AssistantMessage):
            _insert_synthetic()
            if msg.stop_reason in ("error", "aborted"):
                continue
            tool_calls = [b for b in msg.content if b.type == "toolCall"]
            if tool_calls:
                pending_tool_calls = tool_calls
                existing_tool_result_ids = set()
            result.append(msg)
        elif msg.role == "toolResult" and isinstance(msg, ToolResultMessage):
            existing_tool_result_ids.add(msg.tool_call_id)
            result.append(msg)
        elif msg.role == "user":
            _insert_synthetic()
            result.append(msg)
        else:
            result.append(msg)
    _insert_synthetic()
    return result


def convert_responses_messages(
    model: ModelInfo,
    context: Context,
    allowed_tool_call_providers: frozenset[str],
    include_system_prompt: bool = True,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    def _normalize_tool_call_id(
        tool_id: str, _target_model: ModelInfo, source: AssistantMessage
    ) -> str:
        if model.provider not in allowed_tool_call_providers:
            return _normalize_id_part(tool_id)
        if "|" not in tool_id:
            return _normalize_id_part(tool_id)
        call_id, item_id_raw = tool_id.split("|", 1)
        normalized_call_id = _normalize_id_part(call_id)
        is_foreign = source.provider != model.provider or source.api != model.api
        normalized_item_id = (
            _build_foreign_responses_item_id(item_id_raw)
            if is_foreign
            else _normalize_id_part(item_id_raw)
        )
        if not normalized_item_id.startswith("fc_"):
            normalized_item_id = _normalize_id_part(f"fc_{normalized_item_id}")
        return f"{normalized_call_id}|{normalized_item_id}"

    transformed = transform_messages(context.messages, model, _normalize_tool_call_id)

    if include_system_prompt and context.system_prompt:
        role = "developer" if model.reasoning else "system"
        messages.append(
            {
                "role": role,
                "content": [_sanitize_surrogates(context.system_prompt)],
            }
        )

    msg_index = 0
    for msg in transformed:
        if msg.role == "user" and isinstance(msg, UserMessage):
            if isinstance(msg.content, str):
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": _sanitize_surrogates(msg.content),
                            }
                        ],
                    }
                )
            elif isinstance(msg.content, list):
                content: list[dict[str, Any]] = []
                has_text = False
                for item in msg.content:
                    if isinstance(item, TextContent):
                        has_text = True
                        content.append(
                            {
                                "type": "input_text",
                                "text": _sanitize_surrogates(item.text),
                            }
                        )
                    elif isinstance(item, ImageContent):
                        content.append(
                            {
                                "type": "input_image",
                                "detail": "auto",
                                "image_url": f"data:{item.mime_type};base64,{item.data}",
                            }
                        )
                if not content:
                    continue
                messages.append({"role": "user", "content": content})
        elif msg.role == "assistant" and isinstance(msg, AssistantMessage):
            output: list[dict[str, Any]] = []
            is_different_model = (
                msg.model != model.id
                and msg.provider == model.provider
                and msg.api == model.api
            )
            for block in msg.content:
                if isinstance(block, ThinkingContent):
                    if block.thinking_signature:
                        try:
                            output.append(json.loads(block.thinking_signature))
                        except json.JSONDecodeError:
                            pass
                elif isinstance(block, TextContent):
                    parsed_sig = _parse_text_signature(block.text_signature)
                    mid = parsed_sig.get("id") if parsed_sig else None
                    if not mid:
                        mid = f"msg_{msg_index}"
                    elif len(mid) > 64:
                        mid = f"msg_{short_hash(mid)}"
                    msg_item: dict[str, Any] = {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": _sanitize_surrogates(block.text),
                                "annotations": [],
                            }
                        ],
                        "status": "completed",
                        "id": mid,
                    }
                    if parsed_sig and "phase" in parsed_sig:
                        msg_item["phase"] = parsed_sig["phase"]
                    output.append(msg_item)
                elif isinstance(block, ToolCall):
                    parts = block.id.split("|", 1)
                    call_id = parts[0]
                    item_id_raw = parts[1] if len(parts) > 1 else None
                    item_id: str | None = item_id_raw
                    if is_different_model and item_id and item_id.startswith("fc_"):
                        item_id = None
                    tool_item: dict[str, Any] = {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": block.name,
                        "arguments": json.dumps(block.arguments),
                    }
                    if item_id:
                        tool_item["id"] = item_id
                    output.append(tool_item)
            if not output:
                continue
            messages.extend(output)
        elif msg.role == "toolResult" and isinstance(msg, ToolResultMessage):
            text_result = "\n".join(
                c.text for c in msg.content if isinstance(c, TextContent)
            )
            has_images = any(isinstance(c, ImageContent) for c in msg.content)
            has_text = len(text_result) > 0
            call_id = msg.tool_call_id.split("|", 1)[0]
            if has_images and "image" in model.input:
                content_parts: list[dict[str, Any]] = []
                if has_text:
                    content_parts.append(
                        {
                            "type": "input_text",
                            "text": _sanitize_surrogates(text_result),
                        }
                    )
                for cblock in msg.content:
                    if isinstance(cblock, ImageContent):
                        content_parts.append(
                            {
                                "type": "input_image",
                                "detail": "auto",
                                "image_url": f"data:{cblock.mime_type};base64,{cblock.data}",
                            }
                        )
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": content_parts,
                    }
                )
            else:
                output_text = (
                    _sanitize_surrogates(text_result)
                    if has_text
                    else "(see attached image)"
                )
                messages.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output_text,
                    }
                )
        msg_index += 1
    return messages


def convert_responses_tools(
    tools: list[Tool], strict: bool | None = False
) -> list[dict[str, Any]]:
    s = strict if strict is not None else False
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "strict": s,
        }
        for tool in tools
    ]


async def process_responses_stream(
    openai_stream: AsyncIterator[dict[str, Any]],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    model: ModelInfo,
    service_tier: str | None = None,
    resolve_service_tier_fn: Callable[[str | None, str | None], str | None]
    | None = None,
    apply_service_tier_pricing_fn: Callable[[Usage, str | None], None] | None = None,
) -> None:
    current_item: dict[str, Any] | None = None
    current_block_type: str | None = None
    current_block_thinking = ""
    current_block_text = ""
    current_block_tool: dict[str, Any] | None = None
    partial_json_buf = ""

    def block_index() -> int:
        return len(output.content) - 1

    async for event in openai_stream:
        event_type = event.get("type")

        if event_type == "response.created":
            resp = event.get("response")
            if isinstance(resp, dict) and "id" in resp:
                output.response_id = resp["id"]

        elif event_type == "response.output_item.added":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "reasoning":
                current_item = item
                current_block_type = "thinking"
                current_block_thinking = ""
                output.content.append(ThinkingContent(type="thinking", thinking=""))
                stream.push(
                    {
                        "type": "thinking_start",
                        "contentIndex": block_index(),
                        "partial": output.model_copy(deep=True),
                    }
                )
            elif item_type == "message":
                current_item = item
                current_block_type = "text"
                current_block_text = ""
                output.content.append(TextContent(type="text", text=""))
                stream.push(
                    {
                        "type": "text_start",
                        "contentIndex": block_index(),
                        "partial": output.model_copy(deep=True),
                    }
                )
            elif item_type == "function_call":
                current_item = item
                current_block_type = "toolCall"
                call_id = item.get("call_id", "")
                item_id = item.get("id", "")
                name = item.get("name", "")
                args_raw = item.get("arguments", "")
                partial_json_buf = args_raw if isinstance(args_raw, str) else ""
                combined_id = f"{call_id}|{item_id}" if item_id else call_id
                current_block_tool = {
                    "id": combined_id,
                    "name": name,
                    "arguments": parse_streaming_json(partial_json_buf),
                    "partialJson": partial_json_buf,
                }
                output.content.append(
                    ToolCall(
                        type="toolCall",
                        id=combined_id,
                        name=name,
                        arguments=current_block_tool["arguments"],
                    )
                )
                stream.push(
                    {
                        "type": "toolcall_start",
                        "contentIndex": block_index(),
                        "partial": output.model_copy(deep=True),
                    }
                )

        elif event_type == "response.reasoning_summary_part.added":
            if current_item and current_item.get("type") == "reasoning":
                current_item.setdefault("summary", [])
                current_item["summary"].append(event.get("part"))

        elif event_type == "response.reasoning_summary_text.delta":
            if (
                current_item
                and current_item.get("type") == "reasoning"
                and current_block_type == "thinking"
            ):
                current_item.setdefault("summary", [])
                delta = event.get("delta", "")
                current_block_thinking += delta
                if current_item["summary"]:
                    current_item["summary"][-1]["text"] = (
                        current_item["summary"][-1].get("text", "") + delta
                    )
                output.content[-1] = ThinkingContent(
                    type="thinking", thinking=current_block_thinking
                )
                stream.push(
                    {
                        "type": "thinking_delta",
                        "contentIndex": block_index(),
                        "delta": delta,
                        "partial": output.model_copy(deep=True),
                    }
                )

        elif event_type == "response.reasoning_summary_part.done":
            if (
                current_item
                and current_item.get("type") == "reasoning"
                and current_block_type == "thinking"
            ):
                current_item.setdefault("summary", [])
                current_block_thinking += "\n\n"
                delta = "\n\n"
                if current_item["summary"]:
                    current_item["summary"][-1]["text"] = (
                        current_item["summary"][-1].get("text", "") + delta
                    )
                output.content[-1] = ThinkingContent(
                    type="thinking", thinking=current_block_thinking
                )
                stream.push(
                    {
                        "type": "thinking_delta",
                        "contentIndex": block_index(),
                        "delta": delta,
                        "partial": output.model_copy(deep=True),
                    }
                )

        elif event_type == "response.reasoning_text.delta":
            if (
                current_item
                and current_item.get("type") == "reasoning"
                and current_block_type == "thinking"
            ):
                delta = event.get("delta", "")
                current_block_thinking += delta
                output.content[-1] = ThinkingContent(
                    type="thinking", thinking=current_block_thinking
                )
                stream.push(
                    {
                        "type": "thinking_delta",
                        "contentIndex": block_index(),
                        "delta": delta,
                        "partial": output.model_copy(deep=True),
                    }
                )

        elif event_type == "response.content_part.added":
            if current_item and current_item.get("type") == "message":
                current_item.setdefault("content", [])
                part = event.get("part")
                if isinstance(part, dict) and part.get("type") in (
                    "output_text",
                    "refusal",
                ):
                    current_item["content"].append(part)

        elif event_type == "response.output_text.delta":
            if (
                current_item
                and current_item.get("type") == "message"
                and current_block_type == "text"
            ):
                content_list = current_item.get("content", [])
                if not content_list:
                    continue
                last_part = content_list[-1]
                if not isinstance(last_part, dict):
                    continue
                if last_part.get("type") == "output_text":
                    delta = event.get("delta", "")
                    current_block_text += delta
                    last_part["text"] = last_part.get("text", "") + delta
                    output.content[-1] = TextContent(
                        type="text", text=current_block_text
                    )
                    stream.push(
                        {
                            "type": "text_delta",
                            "contentIndex": block_index(),
                            "delta": delta,
                            "partial": output.model_copy(deep=True),
                        }
                    )

        elif event_type == "response.refusal.delta":
            if (
                current_item
                and current_item.get("type") == "message"
                and current_block_type == "text"
            ):
                content_list = current_item.get("content", [])
                if not content_list:
                    continue
                last_part = content_list[-1]
                if not isinstance(last_part, dict):
                    continue
                if last_part.get("type") == "refusal":
                    delta = event.get("delta", "")
                    current_block_text += delta
                    last_part["refusal"] = last_part.get("refusal", "") + delta
                    output.content[-1] = TextContent(
                        type="text", text=current_block_text
                    )
                    stream.push(
                        {
                            "type": "text_delta",
                            "contentIndex": block_index(),
                            "delta": delta,
                            "partial": output.model_copy(deep=True),
                        }
                    )

        elif event_type == "response.function_call_arguments.delta":
            if (
                current_item
                and current_item.get("type") == "function_call"
                and current_block_type == "toolCall"
            ):
                assert current_block_tool is not None
                delta = event.get("delta", "")
                partial_json_buf += delta
                parsed = parse_streaming_json(partial_json_buf)
                current_block_tool["arguments"] = parsed
                current_block_tool["partialJson"] = partial_json_buf
                output.content[-1] = ToolCall(
                    type="toolCall",
                    id=current_block_tool["id"],
                    name=current_block_tool["name"],
                    arguments=parsed,
                )
                stream.push(
                    {
                        "type": "toolcall_delta",
                        "contentIndex": block_index(),
                        "delta": delta,
                        "partial": output.model_copy(deep=True),
                    }
                )

        elif event_type == "response.function_call_arguments.done":
            if (
                current_item
                and current_item.get("type") == "function_call"
                and current_block_type == "toolCall"
            ):
                assert current_block_tool is not None
                prev_partial = partial_json_buf
                args_str = event.get("arguments", "")
                partial_json_buf = args_str
                parsed = parse_streaming_json(args_str)
                current_block_tool["arguments"] = parsed
                if args_str.startswith(prev_partial):
                    extra = args_str[len(prev_partial) :]
                    if extra:
                        stream.push(
                            {
                                "type": "toolcall_delta",
                                "contentIndex": block_index(),
                                "delta": extra,
                                "partial": output.model_copy(deep=True),
                            }
                        )
                output.content[-1] = ToolCall(
                    type="toolCall",
                    id=current_block_tool["id"],
                    name=current_block_tool["name"],
                    arguments=parsed,
                )

        elif event_type == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")

            if item_type == "reasoning" and current_block_type == "thinking":
                summary_list = item.get("summary") or []
                summary_text = "\n\n".join(s.get("text", "") for s in summary_list)
                content_list = item.get("content") or []
                content_text = "\n\n".join(c.get("text", "") for c in content_list)
                final_thinking = summary_text or content_text or current_block_thinking
                try:
                    sig = json.dumps(item)
                except (TypeError, ValueError):
                    sig = json.dumps({})
                output.content[-1] = ThinkingContent(
                    type="thinking",
                    thinking=final_thinking,
                    thinking_signature=sig,
                )
                stream.push(
                    {
                        "type": "thinking_end",
                        "contentIndex": block_index(),
                        "content": final_thinking,
                        "partial": output.model_copy(deep=True),
                    }
                )
                current_block_type = None
                current_item = None

            elif item_type == "message" and current_block_type == "text":
                content_list = item.get("content") or []
                joined = "".join(
                    c.get("text", "")
                    if isinstance(c, dict) and c.get("type") == "output_text"
                    else c.get("refusal", "")
                    if isinstance(c, dict) and c.get("type") == "refusal"
                    else ""
                    for c in content_list
                )
                msg_id = item.get("id", f"msg_{block_index()}")
                phase = item.get("phase")
                output.content[-1] = TextContent(
                    type="text",
                    text=joined,
                    text_signature=_encode_text_signature_v1(msg_id, phase),
                )
                stream.push(
                    {
                        "type": "text_end",
                        "contentIndex": block_index(),
                        "content": joined,
                        "partial": output.model_copy(deep=True),
                    }
                )
                current_block_type = None
                current_item = None

            elif item_type == "function_call":
                args_parsed = (
                    parse_streaming_json(
                        current_block_tool["partialJson"]
                        if current_block_tool
                        else (item.get("arguments") or "{}")
                    )
                    if (current_block_tool and current_block_tool.get("partialJson"))
                    else parse_streaming_json(item.get("arguments") or "{}")
                )
                call_id = item.get("call_id", "")
                item_id = item.get("id", "")
                combined_id = f"{call_id}|{item_id}" if item_id else call_id
                tool_call = ToolCall(
                    type="toolCall",
                    id=combined_id,
                    name=item.get("name", ""),
                    arguments=args_parsed,
                )
                if current_block_tool:
                    output.content[-1] = ToolCall(
                        type="toolCall",
                        id=current_block_tool["id"],
                        name=current_block_tool["name"],
                        arguments=args_parsed,
                    )
                else:
                    output.content.append(tool_call)
                stream.push(
                    {
                        "type": "toolcall_end",
                        "contentIndex": block_index(),
                        "toolCall": tool_call,
                        "partial": output.model_copy(deep=True),
                    }
                )
                current_block_type = None
                current_item = None
                current_block_tool = None

        elif event_type == "response.completed":
            resp = event.get("response")
            if isinstance(resp, dict):
                if resp.get("id"):
                    output.response_id = resp["id"]
                if resp.get("usage"):
                    u = resp["usage"]
                    cached_tokens = 0
                    if isinstance(u.get("input_tokens_details"), dict):
                        cached_tokens = (
                            u["input_tokens_details"].get("cached_tokens", 0) or 0
                        )
                    output.usage = Usage(
                        input=(u.get("input_tokens", 0) or 0) - cached_tokens,
                        output=u.get("output_tokens", 0) or 0,
                        cache_read=cached_tokens,
                        cache_write=0,
                        total_tokens=u.get("total_tokens", 0) or 0,
                        cost=UsageCost(
                            input=0, output=0, cache_read=0, cache_write=0, total=0
                        ),
                    )
                calculate_cost(model, output.usage)
                if apply_service_tier_pricing_fn:
                    resolved = (
                        resolve_service_tier_fn(resp.get("service_tier"), service_tier)
                        if resolve_service_tier_fn
                        else (resp.get("service_tier") or service_tier)
                    )
                    apply_service_tier_pricing_fn(output.usage, resolved)
                status = resp.get("status")
                output.stop_reason = map_stop_reason(status)
                if (
                    any(b.type == "toolCall" for b in output.content)
                    and output.stop_reason == "stop"
                ):
                    output.stop_reason = "toolUse"

        elif event_type == "error":
            raise CodexApiError(
                f"Error Code {event.get('code', '')}: {event.get('message', '')}"
                or "Unknown error"
            )

        elif event_type == "response.failed":
            resp = event.get("response")
            if isinstance(resp, dict):
                err = resp.get("error")
                details = resp.get("incomplete_details")
                if isinstance(err, dict):
                    raise CodexApiError(
                        f"{err.get('code', 'unknown')}: {err.get('message', 'no message')}"
                    )
                if isinstance(details, dict):
                    reason = details.get("reason", "unknown")
                    raise CodexApiError(f"incomplete: {reason}")
            raise CodexApiError("Unknown error (no error details in response)")


def build_request_body(
    model: ModelInfo,
    context: Context,
    options: OpenAICodexResponsesOptions | None = None,
) -> dict[str, Any]:
    messages = convert_responses_messages(
        model, context, CODEX_TOOL_CALL_PROVIDERS, include_system_prompt=False
    )

    body: dict[str, Any] = {
        "model": model.id,
        "store": False,
        "stream": True,
        "instructions": context.system_prompt or "You are a helpful assistant.",
        "input": messages,
        "text": {
            "verbosity": options.text_verbosity
            if options and options.text_verbosity
            else "low"
        },
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": options.session_id if options else None,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    if options and options.temperature is not None:
        body["temperature"] = options.temperature

    if options and options.service_tier is not None:
        body["service_tier"] = options.service_tier

    if context.tools:
        body["tools"] = convert_responses_tools(context.tools, strict=None)

    if options and options.reasoning_effort is not None:
        effort = (
            (
                model.thinking_level_map.get("off", "none")
                if options.reasoning_effort == "none"
                else model.thinking_level_map.get(
                    options.reasoning_effort, options.reasoning_effort
                )
            )
            if model.thinking_level_map
            else options.reasoning_effort
        )
        if effort is not None:
            body["reasoning"] = {
                "effort": effort,
                "summary": options.reasoning_summary or "auto",
            }

    return body


async def process_stream(
    response: httpx.Response,
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    model: ModelInfo,
    options: OpenAICodexResponsesOptions | None = None,
) -> None:
    events = map_codex_events(parse_sse(response))
    st = options.service_tier if options else None
    await process_responses_stream(
        events,
        output,
        stream,
        model,
        service_tier=st,
        resolve_service_tier_fn=resolve_service_tier,
        apply_service_tier_pricing_fn=lambda usage, svc: apply_service_tier_pricing(
            usage, svc, model.id
        ),
    )


async def parse_error_response(
    response_text: str, status: int, status_text: str
) -> tuple[str, str | None]:
    message = response_text or status_text or "Request failed"
    friendly_message: str | None = None
    try:
        parsed = json.loads(response_text)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            code = err.get("code") or err.get("type") or ""
            if (
                re.search(
                    r"usage_limit_reached|usage_not_included|rate_limit_exceeded",
                    code,
                    re.IGNORECASE,
                )
                or status == 429
            ):
                plan_type = err.get("plan_type")
                plan = f" ({plan_type.lower()} plan)" if plan_type else ""
                resets_at = err.get("resets_at")
                mins = (
                    max(0, round((resets_at * 1000 - time.time() * 1000) / 60000))
                    if isinstance(resets_at, (int, float))
                    else None
                )
                when = f" Try again in ~{mins} min." if mins is not None else ""
                friendly_message = (
                    f"You have hit your ChatGPT usage limit{plan}.{when}".strip()
                )
            message = err.get("message") or friendly_message or message
    except (json.JSONDecodeError, TypeError):
        pass
    return message, friendly_message


def get_env_api_key(provider: str) -> str | None:
    env_map: dict[str, str] = {
        "openai-codex": "OPENAI_CODEX_API_KEY",
        "openai": "OPENAI_API_KEY",
    }
    env_var = env_map.get(provider)
    if env_var:
        val = os.environ.get(env_var)
        if val:
            return val
    return None


class CachedWebSocketState:
    def __init__(self) -> None:
        self.last_request_body: dict[str, Any] | None = None
        self.last_response_id: str | None = None
        self.last_response_items: list[dict[str, Any]] | None = None


class CachedWebSocketConnection:
    def __init__(self, socket: ClientConnection) -> None:
        self.socket = socket
        self.busy = True
        self.idle_timer: asyncio.TimerHandle | None = None
        self.continuation: CachedWebSocketState | None = None


_websocket_session_cache: dict[str, CachedWebSocketConnection] = {}
_websocket_sse_fallback_sessions: set[str] = set()
_websocket_cache_lock = asyncio.Lock()


def is_websocket_sse_fallback_active(session_id: str | None) -> bool:
    if not session_id:
        return False
    return session_id in _websocket_sse_fallback_sessions


def record_websocket_sse_fallback(session_id: str | None) -> None:
    if not session_id:
        return


def record_websocket_failure(session_id: str | None, error: Exception) -> None:
    if not session_id:
        return
    _websocket_sse_fallback_sessions.add(session_id)


def _close_websocket_silently(socket: ClientConnection) -> None:
    try:
        asyncio.ensure_future(socket.close(1000, "done"))
    except Exception:
        pass


def _schedule_session_websocket_expiry(
    session_id: str, entry: CachedWebSocketConnection, loop: asyncio.AbstractEventLoop
) -> None:
    if entry.idle_timer:
        entry.idle_timer.cancel()
    entry.idle_timer = loop.call_later(
        SESSION_WEBSOCKET_CACHE_TTL_MS / 1000,
        lambda: _on_idle_timeout(session_id, entry),
    )


def _on_idle_timeout(session_id: str, entry: CachedWebSocketConnection) -> None:
    if entry.busy:
        return
    _close_websocket_silently(entry.socket)
    _websocket_session_cache.pop(session_id, None)


def _is_websocket_reusable(socket: ClientConnection) -> bool:
    try:
        return socket.close_code is None
    except Exception:
        return True


async def _connect_websocket(
    url: str, headers: dict[str, str], cancel_event: asyncio.Event | None = None
) -> ClientConnection:
    extra_headers = {
        k: v
        for k, v in headers.items()
        if k.lower() not in ("accept", "content-type", "openai-beta")
    }
    if "OpenAI-Beta" in headers:
        extra_headers["OpenAI-Beta"] = headers["OpenAI-Beta"]
    extra_headers.pop("accept", None)
    extra_headers.pop("content-type", None)
    try:
        socket = await ws_connect(
            url,
            additional_headers=extra_headers,
        )
        return socket
    except Exception as e:
        raise e


async def _extract_websocket_error(event: object) -> Exception:
    if isinstance(event, Exception):
        return event
    return Exception("WebSocket error")


async def _decode_websocket_data(data: object) -> str | None:
    if isinstance(data, str):
        return data
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return None


async def parse_websocket(
    socket: ClientConnection,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    pending: asyncio.Future[None] | None = None
    done = False
    failed: Exception | None = None
    saw_completion = False

    async def _reader() -> None:
        nonlocal done, failed, saw_completion
        try:
            async for message in socket:
                text: str | None = None
                if isinstance(message, str):
                    text = message
                elif isinstance(message, bytes):
                    text = message.decode("utf-8", errors="replace")
                if text is None:
                    continue
                try:
                    parsed = json.loads(text)
                    if not isinstance(parsed, dict):
                        continue
                    etype = parsed.get("type")
                    if etype in (
                        "response.completed",
                        "response.done",
                        "response.incomplete",
                    ):
                        saw_completion = True
                        done = True
                    queue.append(parsed)
                    if pending and not pending.done():
                        pending.set_result(None)
                except json.JSONDecodeError as cause:
                    if not failed:
                        failed = CodexProtocolError(
                            f"Invalid Codex WebSocket JSON: {format_thrown_value(cause)}",
                            payload=text,
                        )
                    done = True
                    if pending and not pending.done():
                        pending.set_result(None)
                    return
        except Exception as e:
            if saw_completion:
                done = True
            else:
                failed = e if isinstance(e, Exception) else Exception(str(e))
            done = True
            if pending and not pending.done():
                pending.set_result(None)

    reader_task = asyncio.create_task(_reader())

    try:
        while True:
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Request was aborted")
            if queue:
                yield queue.pop(0)
                continue
            if done:
                break
            pending = asyncio.get_event_loop().create_future()
            try:
                await asyncio.wait_for(pending, timeout=0.1)
            except asyncio.TimeoutError:
                continue
            finally:
                pending = None
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass

    if failed:
        raise failed
    if not saw_completion:
        raise CodexProtocolError("WebSocket stream closed before response.completed")


def request_body_without_input(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in ("input", "previous_response_id")}


def response_inputs_equal(
    a: list[dict[str, Any]] | None, b: list[dict[str, Any]] | None
) -> bool:
    return json.dumps(a or []) == json.dumps(b or [])


def request_bodies_match_except_input(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return json.dumps(request_body_without_input(a), sort_keys=True) == json.dumps(
        request_body_without_input(b), sort_keys=True
    )


def get_cached_websocket_input_delta(
    body: dict[str, Any],
    state: CachedWebSocketState,
) -> list[dict[str, Any]] | None:
    if not request_bodies_match_except_input(body, state.last_request_body or {}):
        return None
    current_input = body.get("input", [])
    baseline = list(
        state.last_request_body.get("input", []) if state.last_request_body else []
    ) + list(state.last_response_items or [])
    if len(current_input) < len(baseline):
        return None
    prefix = current_input[: len(baseline)]
    if not response_inputs_equal(prefix, baseline):
        return None
    return current_input[len(baseline) :]


def build_cached_websocket_request_body(
    entry: CachedWebSocketConnection,
    body: dict[str, Any],
) -> dict[str, Any]:
    state = entry.continuation
    if not state:
        return body
    delta = get_cached_websocket_input_delta(body, state)
    if not delta or not state.last_response_id:
        entry.continuation = None
        return body
    return {
        **body,
        "previous_response_id": state.last_response_id,
        "input": delta,
    }


async def start_websocket_output_on_first_event(
    events: AsyncIterator[dict[str, Any]],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    on_start: Callable[[], None],
) -> AsyncIterator[dict[str, Any]]:
    started = False
    async for event in events:
        if not started:
            started = True
            on_start()
            stream.push({"type": "start", "partial": output.model_copy(deep=True)})
        yield event


async def process_websocket_stream(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    output: AssistantMessage,
    stream: AssistantMessageEventStream,
    model: ModelInfo,
    on_start: Callable[[], None],
    options: OpenAICodexResponsesOptions | None = None,
) -> None:
    session_id = options.session_id if options else None
    cancel_event = options.cancel_event if options else None
    transport = options.transport if options else None
    service_tier = options.service_tier if options else None
    use_cached_context = transport in ("websocket-cached", "auto")

    if session_id:
        async with _websocket_cache_lock:
            cached = _websocket_session_cache.get(session_id)
        if cached:
            if cached.idle_timer:
                cached.idle_timer.cancel()
                cached.idle_timer = None
            if not cached.busy and _is_websocket_reusable(cached.socket):
                cached.busy = True
                entry = cached
                socket = cached.socket
            else:
                socket = await _connect_websocket(url, headers, cancel_event)
                entry = None
        else:
            socket = await _connect_websocket(url, headers, cancel_event)
            entry = CachedWebSocketConnection(socket)
            entry.busy = True
            async with _websocket_cache_lock:
                _websocket_session_cache[session_id] = entry
    else:
        socket = await _connect_websocket(url, headers, cancel_event)
        entry = None

    keep_connection = True
    full_body = body

    request_body = full_body
    if use_cached_context and entry and entry.continuation:
        request_body = build_cached_websocket_request_body(entry, full_body)

    try:
        await socket.send(json.dumps({"type": "response.create", **request_body}))
        events = map_codex_events(parse_websocket(socket, cancel_event))
        started_events = start_websocket_output_on_first_event(
            events, output, stream, on_start
        )
        await process_responses_stream(
            started_events,
            output,
            stream,
            model,
            service_tier=service_tier,
            resolve_service_tier_fn=resolve_service_tier,
            apply_service_tier_pricing_fn=lambda usage, svc: apply_service_tier_pricing(
                usage, svc, model.id
            ),
        )
        if cancel_event and cancel_event.is_set():
            keep_connection = False
        elif use_cached_context and entry and output.response_id:
            response_items = [
                item
                for item in convert_responses_messages(
                    model,
                    Context(messages=[output], system_prompt=None),
                    CODEX_TOOL_CALL_PROVIDERS,
                    include_system_prompt=False,
                )
                if item.get("type") != "function_call_output"
            ]
            entry.continuation = CachedWebSocketState()
            entry.continuation.last_request_body = full_body.copy()
            entry.continuation.last_response_id = output.response_id
            entry.continuation.last_response_items = response_items
    except Exception:
        if entry:
            entry.continuation = None
        keep_connection = False
        raise
    finally:
        if session_id and entry:
            if keep_connection and _is_websocket_reusable(entry.socket):
                entry.busy = False
                _schedule_session_websocket_expiry(
                    session_id, entry, asyncio.get_event_loop()
                )
                async with _websocket_cache_lock:
                    _websocket_session_cache[session_id] = entry
            else:
                _close_websocket_silently(entry.socket)
                if entry.idle_timer:
                    entry.idle_timer.cancel()
                async with _websocket_cache_lock:
                    _websocket_session_cache.pop(session_id, None)
        else:
            _close_websocket_silently(socket)


def stream_openai_codex_responses(
    model: ModelInfo,
    context: Context,
    options: OpenAICodexResponsesOptions | None = None,
) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()

    async def _run() -> None:
        cancel_event = options.cancel_event if options else None

        output = AssistantMessage(
            role="assistant",
            content=[],
            api="openai-codex-responses",
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
            if not api_key:
                raise ValueError(f"No API key for provider: {model.provider}")

            account_id = extract_account_id(api_key)
            body = build_request_body(model, context, options)
            if options and options.on_payload:
                next_body = options.on_payload(body, model)
                if next_body is not None:
                    body = next_body

            websocket_request_id = (
                options.session_id if options else None
            ) or create_codex_request_id()

            sse_headers = build_sse_headers(
                model.headers,
                options.headers if options else None,
                account_id,
                api_key,
                options.session_id if options else None,
            )
            websocket_headers = build_websocket_headers(
                model.headers,
                options.headers if options else None,
                account_id,
                api_key,
                websocket_request_id,
            )

            body_json = json.dumps(body)
            transport = options.transport if options else "auto"
            websocket_disabled_for_session = (
                transport != "sse"
                and is_websocket_sse_fallback_active(
                    options.session_id if options else None
                )
            )
            if websocket_disabled_for_session:
                record_websocket_sse_fallback(options.session_id if options else None)

            if transport != "sse" and not websocket_disabled_for_session:
                websocket_started = False

                def _on_ws_start() -> None:
                    nonlocal websocket_started
                    websocket_started = True

                try:
                    ws_url = resolve_codex_websocket_url(model.base_url)
                    await process_websocket_stream(
                        ws_url,
                        body,
                        websocket_headers,
                        output,
                        stream,
                        model,
                        _on_ws_start,
                        options,
                    )

                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError("Request was aborted")
                    stream.push(
                        {
                            "type": "done",
                            "reason": cast(
                                Literal["stop", "length", "toolUse"], output.stop_reason
                            ),
                            "message": output.model_copy(deep=True),
                        }
                    )
                    stream.end()
                    return
                except (asyncio.CancelledError, CodexApiError, CodexProtocolError):
                    raise
                except Exception as error:
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError("Request was aborted")
                    if websocket_started:
                        raise
                    record_websocket_failure(
                        options.session_id if options else None, error
                    )
                    record_websocket_sse_fallback(
                        options.session_id if options else None
                    )

            response: httpx.Response | None = None
            last_error: Exception | None = None
            max_retries = (
                options.max_retries
                if options and options.max_retries is not None
                else MAX_RETRIES
            )
            base_delay = (
                options.max_retry_delay_ms
                if options and options.max_retry_delay_ms is not None
                else BASE_DELAY_MS
            )

            for attempt in range(max_retries + 1):
                if cancel_event and cancel_event.is_set():
                    raise asyncio.CancelledError("Request was aborted")

                sse_url = resolve_codex_url(model.base_url)
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(
                            options.timeout_ms / 1000
                            if options and options.timeout_ms
                            else 120.0
                        )
                    ) as client:
                        response = await client.post(
                            sse_url,
                            headers=sse_headers,
                            content=body_json,
                        )
                    if options and options.on_response:
                        options.on_response(
                            {
                                "status": response.status_code,
                                "headers": dict(response.headers),
                            },
                            model,
                        )

                    if response.is_success:
                        break

                    error_text = response.text
                    if attempt < max_retries and is_retryable_error(
                        response.status_code, error_text
                    ):
                        delay_ms = base_delay * (2**attempt)
                        await sleep_with_cancel(delay_ms, cancel_event)
                        continue

                    message, _ = await parse_error_response(
                        error_text, response.status_code, response.reason_phrase or ""
                    )
                    raise CodexApiError(message)

                except (asyncio.CancelledError, CodexApiError):
                    raise
                except Exception as error:
                    if cancel_event and cancel_event.is_set():
                        raise asyncio.CancelledError("Request was aborted")
                    last_error = (
                        error if isinstance(error, Exception) else Exception(str(error))
                    )
                    if (
                        attempt < max_retries
                        and "usage limit" not in str(last_error).lower()
                    ):
                        delay_ms = base_delay * (2**attempt)
                        await sleep_with_cancel(delay_ms, cancel_event)
                        continue
                    raise last_error

            if not response or not response.is_success:
                raise last_error or CodexApiError("Failed after retries")

            stream.push({"type": "start", "partial": output.model_copy(deep=True)})
            await process_stream(response, output, stream, model, options)

            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Request was aborted")

            stream.push(
                {
                    "type": "done",
                    "reason": cast(
                        Literal["stop", "length", "toolUse"], output.stop_reason
                    ),
                    "message": output.model_copy(deep=True),
                }
            )
            stream.end()

        except Exception as error:
            for block in output.content:
                pass
            output.stop_reason = (
                "aborted" if (cancel_event and cancel_event.is_set()) else "error"
            )
            output.error_message = str(error)
            stream.push(
                {
                    "type": "error",
                    "reason": output.stop_reason,
                    "error": output.model_copy(deep=True),
                }
            )
            stream.end()

    asyncio.create_task(_run())
    return stream


def stream_simple_openai_codex_responses(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    api_key = (options.api_key if options else None) or get_env_api_key(model.provider)
    if not api_key:
        raise ValueError(f"No API key for provider: {model.provider}")

    base_options = OpenAICodexResponsesOptions(
        temperature=options.temperature if options else None,
        max_tokens=options.max_tokens if options else None,
        api_key=api_key,
        transport=options.transport if options else None,
        cache_retention=options.cache_retention if options else None,
        session_id=options.session_id if options else None,
        headers=options.headers if options else None,
        timeout_ms=options.timeout_ms if options else None,
        max_retries=options.max_retries if options else None,
        max_retry_delay_ms=options.max_retry_delay_ms if options else None,
        metadata=options.metadata if options else None,
    )

    if options and options.reasoning:
        clamped = clamp_thinking_level(model, options.reasoning)
        base_options.reasoning_effort = clamped if clamped != "off" else None

    return stream_openai_codex_responses(model, context, base_options)


register_api_provider(
    ApiProvider(
        KnownApi.OPENAI_CODEX_RESPONSES,
        stream_openai_codex_responses,  # type: ignore[arg-type]
        stream_simple_openai_codex_responses,  # type: ignore[arg-type]
    )
)
