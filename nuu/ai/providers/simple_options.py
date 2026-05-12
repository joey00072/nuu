"""
Transforms SimpleStreamOptions (with thinking level) into per-provider
StreamOptions (with provider-specific reasoning/reasoning_effort fields).
Each provider uses these helpers to normalize the simple API.

Owns: simple_options_to_* converters for each provider family.
Delegates to: nuu.ai.types for option model definitions.

Depends on: nuu.ai.types
"""

from __future__ import annotations

from typing import Literal, TypedDict

from ..types import (
    ModelInfo,
    SimpleStreamOptions,
    StreamOptions,
    ThinkingBudgets,
    ThinkingLevel,
)

BasicLevel = Literal["minimal", "low", "medium", "high"]


class AdjustedTokens(TypedDict):
    max_tokens: int
    thinking_budget: int


def build_base_options(
    model: ModelInfo,
    options: SimpleStreamOptions | None = None,
    api_key: str | None = None,
) -> StreamOptions:
    return StreamOptions(
        temperature=options.temperature if options else None,
        max_tokens=(
            options.max_tokens
            if options and options.max_tokens is not None
            else (min(model.max_tokens, 32000) if model.max_tokens > 0 else None)
        ),
        api_key=api_key or (options.api_key if options else None),
        transport=options.transport if options else None,
        cache_retention=options.cache_retention if options else None,
        session_id=options.session_id if options else None,
        headers=options.headers if options else None,
        timeout_ms=options.timeout_ms if options else None,
        max_retries=options.max_retries if options else None,
        max_retry_delay_ms=options.max_retry_delay_ms if options else None,
        metadata=options.metadata if options else None,
    )


def clamp_reasoning(effort: ThinkingLevel | None) -> BasicLevel | None:
    return "high" if effort == "xhigh" else effort


def adjust_max_tokens_for_thinking(
    base_max_tokens: int,
    model_max_tokens: int,
    reasoning_level: ThinkingLevel,
    custom_budgets: ThinkingBudgets | None = None,
) -> AdjustedTokens:
    budget_defaults: dict[str, int] = {
        "minimal": 1024,
        "low": 2048,
        "medium": 8192,
        "high": 16384,
    }
    if custom_budgets:
        for key in budget_defaults:
            val = getattr(custom_budgets, key, None)
            if val is not None:
                budget_defaults[key] = val

    min_output_tokens = 1024
    level: BasicLevel = clamp_reasoning(reasoning_level)  # type: ignore[assignment]
    thinking_budget = budget_defaults[level]
    max_tokens = min(base_max_tokens + thinking_budget, model_max_tokens)

    if max_tokens <= thinking_budget:
        thinking_budget = max(0, max_tokens - min_output_tokens)

    return AdjustedTokens(max_tokens=max_tokens, thinking_budget=thinking_budget)
