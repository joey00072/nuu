"""
In-memory model registry, cost calculator, and thinking-level utilities.
Loads model definitions from models.json at import time.

Owns: _model_registry dict, calculate_cost(), get_supported_thinking_levels(),
  clamp_thinking_level().
Delegates to: json for file loading.

Data flow: models.json -> register_model() -> get_model() / get_models()

Depends on: nuu.ai.types (ModelInfo, Usage, UsageCost)
"""

import json
import pathlib
from .types import (
    ModelInfo,
    ModelThinkingLevel,
    Usage,
    UsageCost,
)

_model_registry: dict[str, dict[str, ModelInfo]] = {}


def load_models():
    models_file = pathlib.Path(__file__).parent / "models.json"
    if not models_file.exists():
        return

    with open(models_file, "r") as f:
        data = json.load(f)

    for provider, models in data.items():
        for model_id, info in models.items():
            register_model(ModelInfo(**info))


def register_model(model: ModelInfo) -> None:
    if model.provider not in _model_registry:
        _model_registry[model.provider] = {}
    _model_registry[model.provider][model.id] = model


def get_model(provider: str, model_id: str) -> ModelInfo | None:
    return _model_registry.get(provider, {}).get(model_id)


def get_providers() -> list[str]:
    return list(_model_registry.keys())


def get_models(provider: str) -> list[ModelInfo]:
    return list(_model_registry.get(provider, {}).values())


def calculate_cost(model: ModelInfo, usage: Usage) -> UsageCost:
    usage.cost.input = (model.cost.input / 1000000) * usage.input
    usage.cost.output = (model.cost.output / 1000000) * usage.output
    usage.cost.cache_read = (model.cost.cache_read / 1000000) * usage.cache_read
    usage.cost.cache_write = (model.cost.cache_write / 1000000) * usage.cache_write
    usage.cost.total = (
        usage.cost.input
        + usage.cost.output
        + usage.cost.cache_read
        + usage.cost.cache_write
    )
    return usage.cost


EXTENDED_THINKING_LEVELS: list[ModelThinkingLevel] = [
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
]


def get_supported_thinking_levels(model: ModelInfo) -> list[ModelThinkingLevel]:
    if not model.reasoning:
        return ["off"]

    levels: list[ModelThinkingLevel] = []
    for level in EXTENDED_THINKING_LEVELS:
        if model.thinking_level_map:
            mapped = model.thinking_level_map.get(level)
            if mapped is None and level in model.thinking_level_map:
                # In Pi, null marks as unsupported. In Python, None.
                continue
        if level == "xhigh":
            if model.thinking_level_map and "xhigh" in model.thinking_level_map:
                levels.append(level)
            continue
        levels.append(level)
    return levels


def clamp_thinking_level(
    model: ModelInfo, level: ModelThinkingLevel
) -> ModelThinkingLevel:
    available_levels = get_supported_thinking_levels(model)
    if level in available_levels:
        return level

    try:
        requested_index = EXTENDED_THINKING_LEVELS.index(level)
    except ValueError:
        return available_levels[0] if available_levels else "off"

    for i in range(requested_index, len(EXTENDED_THINKING_LEVELS)):
        candidate = EXTENDED_THINKING_LEVELS[i]
        if candidate in available_levels:
            return candidate

    for i in range(requested_index - 1, -1, -1):
        candidate = EXTENDED_THINKING_LEVELS[i]
        if candidate in available_levels:
            return candidate

    return available_levels[0] if available_levels else "off"


def models_are_equal(a: ModelInfo | None, b: ModelInfo | None) -> bool:
    if not a or not b:
        return False
    return a.id == b.id and a.provider == b.provider


load_models()
