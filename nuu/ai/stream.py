"""
Top-level streaming API: stream(), complete(), stream_simple(), complete_simple().
Resolves the API provider and delegates to its stream function.

Owns: the public stream/complete entry points.
Delegates to: api_registry for provider resolution, then provider's stream fn.

Data flow: ModelInfo + Context + Options -> _resolve_api_provider() ->
  provider.stream() -> AssistantMessageEventStream

Depends on: nuu.ai.api_registry, nuu.ai.types, nuu.ai.event_stream
"""

from .api_registry import get_api_provider
from .event_stream import AssistantMessageEventStream
from .types import (
    Api,
    AssistantMessage,
    Context,
    ModelInfo,
    SimpleStreamOptions,
    StreamOptions,
)


def _resolve_api_provider(api: Api):
    provider = get_api_provider(api)
    if not provider:
        raise ValueError(f"No API provider registered for api: {api}")
    return provider


def stream(
    model: ModelInfo,
    context: Context,
    options: StreamOptions | None = None,
) -> AssistantMessageEventStream:
    provider = _resolve_api_provider(model.api)
    return provider.stream(model, context, options)


async def complete(
    model: ModelInfo,
    context: Context,
    options: StreamOptions | None = None,
) -> AssistantMessage:
    s = stream(model, context, options)
    return await s.result()


def stream_simple(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    provider = _resolve_api_provider(model.api)
    return provider.stream_simple(model, context, options)


async def complete_simple(
    model: ModelInfo,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessage:
    s = stream_simple(model, context, options)
    return await s.result()
