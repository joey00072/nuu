"""
Auto-registers all built-in provider stream implementations into the API
registry. Called once at startup to populate the provider map.

Owns: the side-effect registration of all built-in providers.
Delegates to: each provider module's stream function, api_registry for storage.

Data flow: module import -> register_api_provider() for each KnownApi

Depends on: nuu.ai.api_registry, all provider submodules
"""

from __future__ import annotations

from ..api_registry import ApiProvider, register_api_provider
from ..types import KnownApi

# Auto-registering providers - import for side effect registration
from . import mistral  # noqa: F401
from . import openai_codex_responses  # noqa: F401

# Providers with explicit register functions
from .amazon_bedrock import register_bedrock_provider
from .anthropic import register_anthropic_provider
from .azure_openai_responses import register as register_azure_openai_responses
from .openai_responses import register as register_openai_responses

# Providers needing inline registration
from .google import stream_google, stream_simple_google
from .google_vertex import stream_google_vertex, stream_simple_google_vertex
from .openai_completions import (
    stream_openai_completions,
    stream_simple_openai_completions,
)


def register_builtin_providers() -> None:
    register_anthropic_provider()
    register_bedrock_provider()
    register_azure_openai_responses()
    register_openai_responses()

    register_api_provider(
        ApiProvider(
            api=KnownApi.OPENAI_COMPLETIONS,
            stream=stream_openai_completions,
            stream_simple=stream_simple_openai_completions,
        )
    )

    register_api_provider(
        ApiProvider(
            api=KnownApi.GOOGLE_GENERATIVE_AI,
            stream=stream_google,
            stream_simple=stream_simple_google,
        )
    )

    register_api_provider(
        ApiProvider(
            api=KnownApi.GOOGLE_VERTEX,
            stream=stream_google_vertex,
            stream_simple=stream_simple_google_vertex,
        )
    )


register_builtin_providers()
