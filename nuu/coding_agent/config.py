"""
Path resolution and configuration constants for the coding agent. Determines
agent directory, config/sessions/skills/cache dirs, and detects the package
manager used for installation.

Owns: get_agent_dir(), get_config_dir(), get_sessions_dir(), VERSION,
  detect_package_manager().
Delegates to: platformdirs (optional), os.environ, sys.

Data flow: environment vars or platform defaults -> Path objects

Depends on: platformdirs (optional), standard library
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from platformdirs import PlatformDirs

    _dirs = PlatformDirs("nuu", False)
    BASE_DIR = Path(_dirs.user_data_dir)
except ImportError:
    BASE_DIR = Path.home() / ".nuu"

VERSION = "0.1.0"
APP_NAME = "nuu"
ENV_AGENT_DIR_VAR = f"{APP_NAME.upper()}_AGENT_DIR"


def get_agent_dir() -> Path:
    env_dir = os.environ.get(ENV_AGENT_DIR_VAR)
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return BASE_DIR


def get_config_dir() -> Path:
    return get_agent_dir() / "config"


def get_sessions_dir() -> Path:
    return get_agent_dir() / "sessions"


def get_skills_dir() -> Path:
    return get_agent_dir() / "skills"


def get_cache_dir() -> Path:
    return get_agent_dir() / "cache"


def get_auth_file() -> Path:
    return get_agent_dir() / "auth.json"


def get_settings_file() -> Path:
    return get_agent_dir() / "settings.json"


def get_models_file() -> Path:
    return get_agent_dir() / "models.json"


def detect_package_manager() -> str:
    uv_vars = [
        "UV_CACHE_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "UV_INTERNAL__UV_VERSION",
    ]
    if any(v in os.environ for v in uv_vars):
        return "uv"

    if "PIPX_HOME" in os.environ or "PIPX_BIN_DIR" in os.environ:
        return "pipx"

    if sys.prefix != sys.base_prefix:
        venv_cfg = Path(sys.prefix) / "pyvenv.cfg"
        if venv_cfg.exists() and "uv" in venv_cfg.read_text("utf-8"):
            return "uv"
        return "pip"

    resolved = str(Path(sys.executable).resolve())
    for prefix in ("/opt/homebrew", "/usr/local", "/home/linuxbrew"):
        if resolved.startswith(prefix):
            return "brew"

    if resolved.startswith("/snap/"):
        return "snap"

    return "unknown"
