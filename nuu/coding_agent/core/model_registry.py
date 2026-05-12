"""
Model registry service wrapping the global nuu.ai.models registry. Provides
search, reasoning model filtering, default model configuration, and file-based
persistence of custom model definitions.

Owns: ModelRegistry class, custom model file load/save.
Delegates to: nuu.ai.models (get_model, get_models, etc.) for the global registry.

Data flow: models.json -> ModelRegistry -> resolve_model() / search_models()

Depends on: nuu.ai.models, nuu.ai.types
"""

from __future__ import annotations

import json
from pathlib import Path

from nuu.ai.models import (
    get_model as _get_model,
    get_models as _get_models,
    get_providers as _get_providers,
    register_model as _register_model,
)
from nuu.ai.types import ModelInfo


class ModelRegistry:
    def __init__(self, models_file: Path | None = None) -> None:
        self._defaults: dict[str, str] = {}
        if models_file is not None and models_file.exists():
            self.load_from_file(models_file)

    def get_model(self, provider: str, model_id: str) -> ModelInfo | None:
        return _get_model(provider, model_id)

    def get_models(self, provider: str) -> list[ModelInfo]:
        return _get_models(provider)

    def get_providers(self) -> list[str]:
        return _get_providers()

    def add_model(self, model: ModelInfo) -> None:
        _register_model(model)

    def add_models(self, models: list[ModelInfo]) -> None:
        for model in models:
            _register_model(model)

    def load_from_file(self, path: Path) -> None:
        with open(path) as f:
            data = json.load(f)
        for provider, models in data.items():
            for model_id, info in models.items():
                _register_model(ModelInfo(**info))

    def save_to_file(self, path: Path) -> None:
        data: dict[str, dict[str, dict]] = {}
        for provider in _get_providers():
            data[provider] = {}
            for model in _get_models(provider):
                data[provider][model.id] = model.model_dump(mode="json")
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def resolve_model(self, provider: str | None, model_id: str | None) -> ModelInfo:
        if provider and model_id:
            model = _get_model(provider, model_id)
            if model:
                return model
        if provider:
            default_id = self._defaults.get(provider)
            if default_id:
                model = _get_model(provider, default_id)
                if model:
                    return model
            models = _get_models(provider)
            if models:
                return models[0]
        if model_id:
            for prov in _get_providers():
                model = _get_model(prov, model_id)
                if model:
                    return model
        if self._defaults:
            for prov, default_id in self._defaults.items():
                model = _get_model(prov, default_id)
                if model:
                    return model
        for prov in _get_providers():
            models = _get_models(prov)
            if models:
                return models[0]
        raise ValueError(
            f"No model found for provider={provider!r} model_id={model_id!r}"
        )

    def get_default_model(self, provider: str) -> ModelInfo | None:
        default_id = self._defaults.get(provider)
        if default_id:
            return _get_model(provider, default_id)
        return None

    def search_models(self, query: str) -> list[ModelInfo]:
        query_lower = query.lower()
        result: list[ModelInfo] = []
        for provider in _get_providers():
            for model in _get_models(provider):
                if query_lower in model.id.lower() or query_lower in model.name.lower():
                    result.append(model)
        return result

    def get_reasoning_models(self) -> list[ModelInfo]:
        result: list[ModelInfo] = []
        for provider in _get_providers():
            for model in _get_models(provider):
                if model.reasoning:
                    result.append(model)
        return result

    def set_default_model(self, provider: str, model_id: str) -> None:
        self._defaults[provider] = model_id
