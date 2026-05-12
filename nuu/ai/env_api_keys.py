"""
Environment-variable-based API key resolution. Maps provider names to env var
names and handles non-standard auth (Google ADC, AWS profile/bearer tokens).

Owns: _API_KEY_ENV_MAP, get_env_api_key(), find_env_keys().
Delegates to: os.environ for key lookup.

Data flow: provider string -> get_env_api_key() -> str | None

Depends on: standard library only (os)
"""

from __future__ import annotations

import os

_API_KEY_ENV_MAP: dict[str, str | list[str]] = {
    "openai": "OPENAI_API_KEY",
    "azure-openai-responses": "AZURE_OPENAI_API_KEY",
    "openai-codex": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    "google-vertex": "GOOGLE_VERTEX_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "xai": "XAI_API_KEY",
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


def get_api_key_env_vars(provider: str) -> list[str]:
    if provider == "anthropic":
        return ["ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]
    if provider == "github-copilot":
        return ["GITHUB_COPILOT_TOKEN"]
    entry = _API_KEY_ENV_MAP.get(provider)
    if entry is None:
        return []
    if isinstance(entry, list):
        return entry
    return [entry]


def find_env_keys(provider: str) -> list[str]:
    return [v for v in get_api_key_env_vars(provider) if os.environ.get(v) is not None]


def get_env_api_key(provider: str) -> str | None:
    keys = find_env_keys(provider)
    if keys:
        value = os.environ.get(keys[0])
        if value is not None:
            return value

    if provider == "google-vertex":
        gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if gac_path is not None:
            has_adc = os.path.exists(gac_path)
        else:
            has_adc = os.path.exists(
                os.path.join(
                    os.path.expanduser("~"),
                    ".config",
                    "gcloud",
                    "application_default_credentials.json",
                )
            )
        has_project = (
            os.environ.get("GOOGLE_CLOUD_PROJECT") is not None
            or os.environ.get("GCLOUD_PROJECT") is not None
        )
        has_location = os.environ.get("GOOGLE_CLOUD_LOCATION") is not None
        if has_adc and has_project and has_location:
            return "<authenticated>"

    if provider == "amazon-bedrock":
        if (
            os.environ.get("AWS_PROFILE") is not None
            or (
                os.environ.get("AWS_ACCESS_KEY_ID") is not None
                and os.environ.get("AWS_SECRET_ACCESS_KEY") is not None
            )
            or os.environ.get("AWS_BEARER_TOKEN_BEDROCK") is not None
            or os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") is not None
            or os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI") is not None
            or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE") is not None
        ):
            return "<authenticated>"

    return None
