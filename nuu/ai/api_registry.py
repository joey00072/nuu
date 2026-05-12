"""
Registry mapping API identifiers to their stream implementations. Provides the
lookup layer that stream() and stream_simple() use to dispatch calls.

Owns: the ApiProvider protocol, the in-memory _api_provider_registry dict.
Delegates to: provider modules (e.g., anthropic, openai_responses) for
  actual streaming logic.

Data flow: API name -> get_api_provider() -> ApiProvider.stream() / .stream_simple()

Depends on: nuu.ai.types, nuu.ai.event_stream
"""

from typing import Protocol, TypeVar

from .event_stream import AssistantMessageEventStream
from .types import Api, Context, ModelInfo, StreamOptions, SimpleStreamOptions

TOptions = TypeVar("TOptions", bound=StreamOptions, contravariant=True)


class StreamFunction(Protocol[TOptions]):
    def __call__(
        self,
        model: ModelInfo,
        context: Context,
        options: TOptions | None = None,
    ) -> AssistantMessageEventStream: ...


class ApiProvider:
    def __init__(
        self,
        api: Api,
        stream: StreamFunction[StreamOptions],
        stream_simple: StreamFunction[SimpleStreamOptions],
    ):
        self.api = api
        self.stream = stream
        self.stream_simple = stream_simple


_api_provider_registry: dict[str, ApiProvider] = {}


def register_api_provider(provider: ApiProvider) -> None:
    _api_provider_registry[provider.api] = provider


def get_api_provider(api: Api) -> ApiProvider | None:
    return _api_provider_registry.get(api)


def get_api_providers() -> list[ApiProvider]:
    return list(_api_provider_registry.values())


def clear_api_providers() -> None:
    _api_provider_registry.clear()
