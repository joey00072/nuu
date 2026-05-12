"""
Legacy settings manager (used before core/settings_manager.py). Supports
per-project .nuu/settings.json override loading.

Owns: SettingsManager class, settings file I/O, project-level merging.
Delegates to: json for serialization, pathlib for file paths.

Depends on: pydantic, json, os, pathlib
"""

import json
import os
from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class CompactionSettings(BaseModel):
    enabled: bool = True
    reserve_tokens: int = 16384
    keep_recent_tokens: int = 20000


class Settings(BaseModel):
    default_provider: Optional[str] = None
    default_model: Optional[str] = None
    default_thinking_level: Literal[
        "off", "minimal", "low", "medium", "high", "xhigh"
    ] = "off"
    compaction: CompactionSettings = Field(default_factory=CompactionSettings)
    skills: List[str] = Field(default_factory=list)
    prompts: List[str] = Field(default_factory=list)
    session_dir: Optional[str] = None


class SettingsManager:
    def __init__(self, cwd: str, agent_dir: str):
        self.cwd = cwd
        self.agent_dir = agent_dir
        self.global_settings_path = os.path.join(agent_dir, "settings.json")
        self.project_settings_path = os.path.join(cwd, ".nuu", "settings.json")

        self.global_settings = self._load_settings(self.global_settings_path)
        self.project_settings = self._load_settings(self.project_settings_path)

        self.settings = self._merge_settings(
            self.global_settings, self.project_settings
        )

    def _load_settings(self, path: str) -> Settings:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return Settings(**data)
            except Exception:
                pass
        return Settings()

    def _merge_settings(self, base: Settings, overrides: Settings) -> Settings:
        # Simple merge for now: overrides wins for top-level fields
        # In a real impl, we'd do deep merge for dicts
        base_dict = base.model_dump()
        overrides_dict = overrides.model_dump(exclude_unset=True)

        # Manually merge compaction if set in overrides
        if "compaction" in overrides_dict:
            base_dict["compaction"].update(overrides_dict["compaction"])
            del overrides_dict["compaction"]

        base_dict.update(overrides_dict)
        return Settings(**base_dict)

    def save_global(self):
        os.makedirs(self.agent_dir, exist_ok=True)
        with open(self.global_settings_path, "w", encoding="utf-8") as f:
            f.write(self.global_settings.model_dump_json(indent=2))

    def save_project(self):
        os.makedirs(os.path.dirname(self.project_settings_path), exist_ok=True)
        with open(self.project_settings_path, "w", encoding="utf-8") as f:
            f.write(self.project_settings.model_dump_json(indent=2))

    @staticmethod
    def create(cwd: str, agent_dir: Optional[str] = None) -> "SettingsManager":
        if not agent_dir:
            agent_dir = os.path.expanduser("~/.nuu")
        return SettingsManager(cwd, agent_dir)
