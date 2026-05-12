"""
Fake/test provider that emits deterministic or configurable responses without
network calls. Used for tests, demos, and offline development.

Owns: stream_faux(), stream_simple_faux().
Delegates to: nothing (pure in-memory generation).

Data flow: Context -> predefined response patterns -> AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.event_stream
"""

import asyncio
import time
import uuid
from typing import Any, Literal, Union

from ..api_registry import ApiProvider, register_api_provider
from ..event_stream import AssistantMessageEventStream
from ..types import (
    AssistantMessage,
    Context,
    ModelCost,
    ModelInfo,
    StreamOptions,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
    UsageCost,
)

DEFAULT_API = "faux"
DEFAULT_PROVIDER = "faux"
DEFAULT_MODEL_ID = "faux-1"
DEFAULT_USAGE = Usage(
    input=0,
    output=0,
    cache_read=0,
    cache_write=0,
    total_tokens=0,
    cost=UsageCost(input=0, output=0, cache_read=0, cache_write=0, total=0),
)


def faux_text(text: str) -> TextContent:
    return TextContent(type="text", text=text)


def faux_thinking(thinking: str) -> ThinkingContent:
    return ThinkingContent(type="thinking", thinking=thinking)


def faux_tool_call(
    name: str, arguments: dict[str, Any], tool_id: str | None = None
) -> ToolCall:
    return ToolCall(
        type="toolCall",
        id=tool_id or f"tool-{uuid.uuid4()}",
        name=name,
        arguments=arguments,
    )


def faux_assistant_message(
    content: str | list[Union[TextContent, ThinkingContent, ToolCall]],
    stop_reason: Literal["stop", "length", "toolUse", "error", "aborted"] = "stop",
    error_message: str | None = None,
) -> AssistantMessage:
    if isinstance(content, str):
        content = [faux_text(content)]
    return AssistantMessage(
        role="assistant",
        content=content,
        api=DEFAULT_API,
        provider=DEFAULT_PROVIDER,
        model=DEFAULT_MODEL_ID,
        usage=DEFAULT_USAGE,
        stop_reason=stop_reason,
        error_message=error_message,
        timestamp=int(time.time() * 1000),
    )


class FauxProviderRegistration:
    def __init__(self, api: str, models: list[ModelInfo]):
        self.api = api
        self.models = models
        self.responses: list[AssistantMessage] = []

    def set_responses(self, responses: list[AssistantMessage]):
        self.responses = responses.copy()

    def get_model(self, model_id: str | None = None) -> ModelInfo:
        if not model_id:
            return self.models[0]
        for m in self.models:
            if m.id == model_id:
                return m
        return self.models[0]


def register_faux_provider(
    api: str = DEFAULT_API, provider: str = DEFAULT_PROVIDER
) -> FauxProviderRegistration:
    model = ModelInfo(
        id=DEFAULT_MODEL_ID,
        name="Faux Model",
        api=api,
        provider=provider,
        base_url="http://localhost:0",
        reasoning=True,
        input=["text", "image"],
        cost=ModelCost(input=0, output=0, cache_read=0, cache_write=0),
        context_window=128000,
        max_tokens=16384,
    )

    reg = FauxProviderRegistration(api, [model])

    def stream(
        model_info: ModelInfo,
        context: Context,
        options: StreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        s = AssistantMessageEventStream()

        async def _run():
            if not reg.responses:
                err = faux_assistant_message(
                    "No faux responses queued", stop_reason="error"
                )
                s.push({"type": "error", "reason": "error", "error": err})
                s.end(err)
                return

            msg = reg.responses.pop(0)
            # Simplified: just push the whole message as "done"
            s.push(
                {"type": "start", "partial": msg, "contentIndex": None, "delta": None}
            )
            s.push({"type": "done", "reason": msg.stop_reason, "message": msg})
            s.end(msg)

        asyncio.create_task(_run())
        return s

    register_api_provider(ApiProvider(api, stream, stream))
    return reg
