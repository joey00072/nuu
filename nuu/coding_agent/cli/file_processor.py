"""
File processing for CLI input. Handles reading from stdin, expanding file
include directives (e.g., @filename), and processing initial context files.

Owns: read_stdin(), file include expansion.
Delegates to: os, sys, pathlib for file I/O.

Data flow: stdin or @file directives -> processed text content

Depends on: standard library only (os, sys, pathlib)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NamedTuple


class ProcessedFiles(NamedTuple):
    text: str
    file_paths: list[str]


def process_cli_files(file_paths: list[str]) -> ProcessedFiles:
    text_parts: list[str] = []
    resolved_paths: list[str] = []

    for raw_path in file_paths:
        abs_path = str(Path(raw_path).expanduser().resolve())

        if not os.path.exists(abs_path):
            print(f"Warning: File not found: {abs_path}", file=sys.stderr)
            continue

        if os.path.getsize(abs_path) == 0:
            continue

        resolved_paths.append(abs_path)

        try:
            content = Path(abs_path).read_text("utf-8")
            text_parts.append(f'<file name="{abs_path}">\n{content}\n</file>')
        except (OSError, UnicodeDecodeError) as e:
            print(f"Warning: Could not read file {abs_path}: {e}", file=sys.stderr)
            continue

    return ProcessedFiles(text="\n".join(text_parts), file_paths=resolved_paths)


def read_stdin() -> str | None:
    if sys.stdin.isatty():
        return None

    try:
        data = sys.stdin.read()
        return data if data.strip() else None
    except Exception:
        return None
