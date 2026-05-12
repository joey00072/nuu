"""
Human-readable display names for built-in providers. Used by UI components,
CLI output, and session display to show friendly provider names.

Owns: BUILT_IN_PROVIDER_DISPLAY_NAMES dict.
Delegates to: nothing (static data only).

Depends on: nothing (standard library only)
"""

from __future__ import annotations

BUILT_IN_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "amazon-bedrock": "Amazon Bedrock",
    "anthropic": "Anthropic",
    "azure-openai-responses": "Azure OpenAI Responses",
    "cerebras": "Cerebras",
    "cloudflare-ai-gateway": "Cloudflare AI Gateway",
    "cloudflare-workers-ai": "Cloudflare Workers AI",
    "deepseek": "DeepSeek",
    "fireworks": "Fireworks",
    "github-copilot": "GitHub Copilot",
    "google": "Google Gemini",
    "google-vertex": "Google Vertex AI",
    "groq": "Groq",
    "huggingface": "Hugging Face",
    "kimi-coding": "Kimi Coding",
    "minimax": "MiniMax",
    "minimax-cn": "MiniMax (China)",
    "mistral": "Mistral",
    "moonshotai": "Moonshot AI",
    "moonshotai-cn": "Moonshot AI (China)",
    "openai": "OpenAI",
    "openai-codex": "OpenAI Codex",
    "opencode": "OpenCode",
    "opencode-go": "OpenCode Go",
    "openrouter": "OpenRouter",
    "vercel-ai-gateway": "Vercel AI Gateway",
    "xai": "xAI",
    "xiaomi": "Xiaomi",
    "xiaomi-token-plan-ams": "Xiaomi (AMS)",
    "xiaomi-token-plan-cn": "Xiaomi (China)",
    "xiaomi-token-plan-sgp": "Xiaomi (SGP)",
    "zai": "ZAI",
}


def get_provider_display_name(provider: str) -> str:
    return BUILT_IN_PROVIDER_DISPLAY_NAMES.get(provider, provider)


def get_api_key_login_prompt(provider: str) -> str:
    display_name = get_provider_display_name(provider)
    return f"Enter your {display_name} API key:"
