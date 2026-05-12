"""
Cloudflare Workers AI and Cloudflare AI Gateway provider. Adapts Cloudflare's
REST API into the standard streaming interface.

Owns: stream_cloudflare(), stream_simple_cloudflare().
Delegates to: httpx for HTTP, Cloudflare REST API.

Data flow: ModelInfo + Context + Options -> Cloudflare API -> AssistantMessageEvent

Depends on: nuu.ai.types, nuu.ai.event_stream, httpx
"""

from __future__ import annotations

import os
import re

from ..types import ModelInfo

CLOUDFLARE_WORKERS_AI_BASE_URL = (
    "https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1"
)

CLOUDFLARE_AI_GATEWAY_COMPAT_BASE_URL = "https://gateway.ai.cloudflare.com/v1/{CLOUDFLARE_ACCOUNT_ID}/{CLOUDFLARE_GATEWAY_ID}/compat"

CLOUDFLARE_AI_GATEWAY_OPENAI_BASE_URL = "https://gateway.ai.cloudflare.com/v1/{CLOUDFLARE_ACCOUNT_ID}/{CLOUDFLARE_GATEWAY_ID}/openai"

CLOUDFLARE_AI_GATEWAY_ANTHROPIC_BASE_URL = "https://gateway.ai.cloudflare.com/v1/{CLOUDFLARE_ACCOUNT_ID}/{CLOUDFLARE_GATEWAY_ID}/anthropic"


def is_cloudflare_provider(provider: str) -> bool:
    return provider in ("cloudflare-workers-ai", "cloudflare-ai-gateway")


def resolve_cloudflare_base_url(model: ModelInfo) -> str:
    url = model.base_url
    if "{" not in url:
        return url

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if value is None:
            raise ValueError(
                f"{name} is required for provider {model.provider} but is not set."
            )
        return value

    return re.sub(r"\{([A-Z_][A-Z0-9_]*)\}", _replace, url)
