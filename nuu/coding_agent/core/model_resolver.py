"""
Model resolution logic. Given provider + model ID patterns (e.g., "anthropic/claude-3"),
resolves to a ModelInfo from the registry. Falls back to defaults and first
available model if exact match fails.

Owns: resolve_model() logic, default model mapping per provider.
Delegates to: nuu.ai.models for registry lookups.

Data flow: provider + model_id pattern -> resolve_model() -> ModelInfo

Depends on: nuu.ai.models (get_model, get_models, get_providers)
"""

from __future__ import annotations

from nuu.ai.models import (
    get_model as _get_model,
    get_models as _get_models,
    get_providers as _get_providers,
)
from nuu.ai.types import ModelInfo, ModelThinkingLevel

from .model_registry import ModelRegistry

DEFAULT_MODEL_PER_PROVIDER: dict[str, str] = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-20250514",
    "google": "gemini-2.5-flash",
    "deepseek": "deepseek-chat",
    "mistral": "mistral-large-latest",
    "amazon-bedrock": "us.anthropic.claude-sonnet-4-20250514",
    "azure-openai-responses": "gpt-4o",
    "openai-codex": "gpt-4o",
    "github-copilot": "gpt-4o",
    "openrouter": "openai/gpt-4o",
    "vercel-ai-gateway": "openai/gpt-4o",
    "xai": "grok-2-latest",
    "groq": "llama-3.3-70b-versatile",
    "cerebras": "llama-3.3-70b",
    "zai": "glm-4-plus",
    "minimax": "minimax-text-01",
    "minimax-cn": "minimax-text-01",
    "moonshotai": "moonshot-v1-8k",
    "moonshotai-cn": "moonshot-v1-8k",
    "huggingface": "meta-llama/Llama-3.3-70B-Instruct",
    "fireworks": "accounts/fireworks/models/llama-v3p3-70b-instruct",
    "opencode": "gpt-4o",
    "opencode-go": "gpt-4o",
    "kimi-coding": "kimi-latest",
    "cloudflare-workers-ai": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "cloudflare-ai-gateway": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "xiaomi": "mi-llama-3.3-70b",
    "xiaomi-token-plan-cn": "mi-llama-3.3-70b",
    "xiaomi-token-plan-ams": "mi-llama-3.3-70b",
    "xiaomi-token-plan-sgp": "mi-llama-3.3-70b",
}


def resolve_model_from_args(
    args: dict,
    model_registry: ModelRegistry,
    settings: object | None = None,
) -> ModelInfo:
    provider: str | None = args.get("provider")
    model_id: str | None = args.get("model")

    if model_id:
        return model_registry.resolve_model(provider, model_id)

    if provider:
        model = model_registry.get_default_model(provider)
        if model:
            return model
        models = _get_models(provider)
        if models:
            return models[0]

    if settings is not None:
        try:
            settings_provider = (
                settings.get_default_provider()
                if hasattr(settings, "get_default_provider")
                else None
            )
            settings_model = (
                settings.get_default_model()
                if hasattr(settings, "get_default_model")
                else None
            )
        except Exception:
            settings_provider = None
            settings_model = None
        if settings_provider and settings_model:
            model = _get_model(settings_provider, settings_model)
            if model:
                return model
        if settings_provider:
            model = model_registry.get_default_model(settings_provider)
            if model:
                return model
            models = _get_models(settings_provider)
            if models:
                return models[0]

    for prov, default_id in DEFAULT_MODEL_PER_PROVIDER.items():
        model = _get_model(prov, default_id)
        if model:
            return model

    for prov in _get_providers():
        models = _get_models(prov)
        if models:
            return models[0]

    raise ValueError("No model could be resolved")


def resolve_thinking_level(args: dict) -> ModelThinkingLevel:
    level = args.get("thinking")
    if level is not None:
        return level
    return "off"
