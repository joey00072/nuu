"""
Path utility helpers for coding agent tools. Handles normalization,
whitespace handling (unicode spaces), and common path operations.

Owns: path normalization, unicode space detection.
Delegates to: os, pathlib for path resolution.

Depends on: standard library only (os, pathlib, re)
"""

import os
import pathlib

UNICODE_SPACES = r"[\u00A0\u2000-\u200A\u202F\u205F\u3000]"
NARROW_NO_BREAK_SPACE = "\u202f"


def expand_path(file_path: str) -> str:
    # Basic ~ expansion
    if file_path == "~":
        return str(pathlib.Path.home())
    if file_path.startswith("~/"):
        return str(pathlib.Path.home() / file_path[2:])

    # Handle @ prefix as in Pi
    if file_path.startswith("@"):
        file_path = file_path[1:]

    return file_path


def resolve_to_cwd(file_path: str, cwd: str) -> str:
    expanded = expand_path(file_path)
    path = pathlib.Path(expanded)
    if path.is_absolute():
        return str(path)
    return str((pathlib.Path(cwd) / path).resolve())


def resolve_read_path(file_path: str, cwd: str) -> str:
    resolved = resolve_to_cwd(file_path, cwd)
    if os.path.exists(resolved):
        return resolved

    # macOS specific variants could be added here if needed
    # For now, just return resolved
    return resolved
