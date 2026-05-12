"""
Re-exports all built-in provider stream implementations and option types.
Each provider module exposes stream_<name>(), stream_simple_<name>(), and
option types for direct use or registry registration.

Owns: the public import path for all provider implementations.
Delegates to: individual provider modules for actual streaming.

Depends on: all submodules in nuu.ai.providers
"""

from .amazon_bedrock import stream_bedrock, stream_simple_bedrock, BedrockOptions
from .anthropic import stream_anthropic, stream_simple_anthropic, AnthropicOptions
from .azure_openai_responses import (
    stream_azure_openai_responses,
    stream_simple_azure_openai_responses,
    AzureOpenAIResponsesOptions,
)
from .cloudflare import is_cloudflare_provider, resolve_cloudflare_base_url
from .faux import register_faux_provider, FauxProviderRegistration
from .github_copilot_headers import (
    build_copilot_dynamic_headers,
    infer_copilot_initiator,
    has_copilot_vision_input,
)
from .google import stream_google, stream_simple_google, GoogleOptions
from .google_shared import convert_messages, convert_tools, sanitize_surrogates
from .google_vertex import (
    stream_google_vertex,
    stream_simple_google_vertex,
    GoogleVertexOptions,
)
from .mistral import stream_mistral, stream_simple_mistral, MistralOptions
from .openai_codex_responses import (
    stream_openai_codex_responses,
    stream_simple_openai_codex_responses,
    OpenAICodexResponsesOptions,
)
from .openai_completions import (
    stream_openai_completions,
    stream_simple_openai_completions,
    OpenAICompletionsOptions,
)
from .openai_responses import (
    stream_openai_responses,
    stream_simple_openai_responses,
    OpenAIResponsesOptions,
)
from .openai_responses_shared import (
    convert_responses_messages,
    convert_responses_tools,
    process_responses_stream,
)
from .simple_options import (
    build_base_options,
    clamp_reasoning,
    adjust_max_tokens_for_thinking,
)
from .transform_messages import transform_messages as transform_messages_util

__all__ = [
    "stream_bedrock",
    "stream_simple_bedrock",
    "BedrockOptions",
    "stream_anthropic",
    "stream_simple_anthropic",
    "AnthropicOptions",
    "stream_azure_openai_responses",
    "stream_simple_azure_openai_responses",
    "AzureOpenAIResponsesOptions",
    "is_cloudflare_provider",
    "resolve_cloudflare_base_url",
    "register_faux_provider",
    "FauxProviderRegistration",
    "build_copilot_dynamic_headers",
    "infer_copilot_initiator",
    "has_copilot_vision_input",
    "stream_google",
    "stream_simple_google",
    "GoogleOptions",
    "convert_messages",
    "convert_tools",
    "sanitize_surrogates",
    "stream_google_vertex",
    "stream_simple_google_vertex",
    "GoogleVertexOptions",
    "stream_mistral",
    "stream_simple_mistral",
    "MistralOptions",
    "stream_openai_codex_responses",
    "stream_simple_openai_codex_responses",
    "OpenAICodexResponsesOptions",
    "stream_openai_completions",
    "stream_simple_openai_completions",
    "OpenAICompletionsOptions",
    "stream_openai_responses",
    "stream_simple_openai_responses",
    "OpenAIResponsesOptions",
    "convert_responses_messages",
    "convert_responses_tools",
    "process_responses_stream",
    "build_base_options",
    "clamp_reasoning",
    "adjust_max_tokens_for_thinking",
    "transform_messages_util",
]
