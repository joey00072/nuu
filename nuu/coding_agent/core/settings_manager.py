"""
Typed settings manager for the coding agent. Loads global settings from
settings.json, deep-merges with per-project .nuu/settings.json overrides,
and provides typed access via Pydantic Settings model.

Owns: Settings model (with nested sub-models), SettingsManager class.
Delegates to: pydantic for validation, json for file I/O.

Data flow: settings.json + .nuu/settings.json -> deep merge -> Settings model

Depends on: pydantic, json, os, tempfile
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError


def _deep_merge_dict(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = base.copy()
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = value
    return result


class CompactionSettings(BaseModel):
    enabled: bool = True
    threshold_tokens: int = 64000
    reserve_tokens: int = 8000
    max_summary_tokens: int = 4000
    summary_model: str = ""


class BranchSummarySettings(BaseModel):
    enabled: bool = True


class RetrySettings(BaseModel):
    max_retries: int = 3
    initial_delay_ms: int = 1000
    max_delay_ms: int = 30000


class TerminalSettings(BaseModel):
    colors: bool = True
    ansi_output: bool = True
    bell: bool = False


class ImageSettings(BaseModel):
    max_images: int = 10
    max_image_size: int = 20_000_000
    image_quality: int = 85


class ThinkingBudgetsSettings(BaseModel):
    minimal: int = 1024
    low: int = 2048
    medium: int = 8192
    high: int = 16384


class MarkdownSettings(BaseModel):
    enabled: bool = True
    wrap_code_blocks: bool = True
    max_output_height: int | None = None


class WarningSettings(BaseModel):
    enabled: bool = True
    api_key_warnings: bool = True
    destructive_operations: bool = True


class PackageSource(BaseModel):
    type: str = "pip"
    name: str = ""
    path: str = ""


class Settings(BaseModel):
    compaction: CompactionSettings = CompactionSettings()
    branch_summary: BranchSummarySettings = BranchSummarySettings()
    retry: RetrySettings = RetrySettings()
    terminal: TerminalSettings = TerminalSettings()
    images: ImageSettings = ImageSettings()
    thinking_budgets: ThinkingBudgetsSettings = ThinkingBudgetsSettings()
    markdown: MarkdownSettings = MarkdownSettings()
    warnings: WarningSettings = WarningSettings()
    packages: list[PackageSource] = []
    last_changelog_version: str = ""
    default_provider: str = ""
    default_model: str = ""
    enabled: bool = True
    steering_mode: str = "one-at-a-time"
    followup_mode: str = "one-at-a-time"
    transport: str = "auto"
    hide_thinking: bool = False
    collapse_changelog: bool = False
    steering_mode: str = "one-at-a-time"
    followup_mode: str = "one-at-a-time"
    transport: str = "auto"
    hide_thinking: bool = False
    collapse_changelog: bool = False
    enabled_models: list[str] | None = None


class SettingsManager:
    def __init__(self, settings_file: Path) -> None:
        self._settings_file = settings_file.resolve()
        self._global_data: dict[str, Any] = {}
        self._project_data: dict[str, Any] = {}
        self._settings = Settings()
        self._load()

    def _load(self) -> None:
        if self._settings_file.exists():
            try:
                self._global_data = json.loads(self._settings_file.read_text("utf-8"))
            except json.JSONDecodeError:
                self._global_data = {}
        self._recompute()

    def _recompute(self) -> None:
        merged = _deep_merge_dict(self._global_data, self._project_data)
        try:
            self._settings = Settings(**merged)
        except ValidationError:
            self._settings = Settings()

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self._settings, key, default)

    def set(self, key: str, value: Any) -> None:
        self._global_data[key] = value
        self._recompute()

    def save(self) -> None:
        dir_path = self._settings_file.parent
        dir_path.mkdir(parents=True, exist_ok=True)
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(dir_path), prefix=".settings", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._global_data, f, indent=2)
            Path(tmp_path_str).replace(self._settings_file)
        except BaseException:
            Path(tmp_path_str).unlink(missing_ok=True)
            raise

    def get_enabled_models(self) -> list[str] | None:
        return self._settings.enabled_models

    def set_enabled_models(self, enabled: list[str] | None) -> None:
        self._global_data["enabled_models"] = enabled
        self._recompute()

    def merge_project_settings(self, project_dir: Path) -> None:
        project_file = project_dir / ".nuu" / "settings.json"
        if project_file.exists():
            try:
                self._project_data = json.loads(project_file.read_text("utf-8"))
            except json.JSONDecodeError:
                self._project_data = {}
        else:
            self._project_data = {}
        self._recompute()

    def get_compaction_settings(self) -> CompactionSettings:
        return self._settings.compaction

    def get_retry_settings(self) -> RetrySettings:
        return self._settings.retry

    def get_image_settings(self) -> ImageSettings:
        return self._settings.images

    def get_thinking_budgets(self) -> dict[str, int]:
        return self._settings.thinking_budgets.model_dump()

    def reset_to_defaults(self) -> None:
        self._global_data = {}
        self._project_data = {}
        self._settings = Settings()

    def get_all(self) -> dict[str, Any]:
        return self._settings.model_dump(mode="json")
