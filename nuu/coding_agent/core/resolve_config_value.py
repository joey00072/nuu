"""
Config value resolution with env var substitution. Resolves config values
that may reference environment variables using ${VAR_NAME} syntax.

Owns: resolve_config_value(), env var interpolation logic.
Delegates to: os.environ for variable lookup, re for pattern matching.

Depends on: standard library only (os, re, pathlib)
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


_ENV_VAR_REF_PATTERN = re.compile(r"^\$(\w+)$|^\$\{(\w+)\}$")


def is_env_var_ref(value: str) -> bool:
    return _ENV_VAR_REF_PATTERN.match(value) is not None


def resolve_env_var_ref(value: str) -> str | None:
    m = _ENV_VAR_REF_PATTERN.match(value)
    if not m:
        return None
    return os.environ.get(m.group(1) or m.group(2))


def resolve_config_value(value: str, env_var_name: str | None = None) -> str | None:
    if env_var_name is not None:
        env_val = os.environ.get(env_var_name)
        if env_val is not None:
            return env_val

    if is_env_var_ref(value):
        resolved = resolve_env_var_ref(value)
        if resolved is not None:
            return resolved

    path = Path(value)
    if path.exists():
        return path.read_text()

    return value


def resolve_config_object(obj: Any, env_map: dict[str, str] | None = None) -> Any:
    if env_map is None:
        env_map = {}
    if isinstance(obj, str):
        return resolve_config_value(obj)
    if isinstance(obj, dict):
        return {k: resolve_config_object(v, env_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_config_object(item, env_map) for item in obj]
    return obj
