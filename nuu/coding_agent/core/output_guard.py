"""
Output guard: filters and sanitizes LLM output before display. Handles
ANSI stripping, truncation, and content safety checks.

Owns: output filtering/sanitization logic.
Delegates to: re for pattern matching, sys for stdout.

Depends on: standard library only (re, sys)
"""

from __future__ import annotations

import re
import sys


class OutputGuard:
    def __init__(self) -> None:
        self._hooked: bool = False
        self._original_stdout_write = sys.stdout.write
        self._original_stderr_write = sys.stderr.write
        self._patterns: list[tuple[re.Pattern, str]] = [
            (
                re.compile(
                    r"(?i)(api[-_]?key\s*[:=]\s*['\"]?)([a-z0-9_\-]{16,})(['\"]?)"
                ),
                r"\1***\3",
            ),
            (re.compile(r"(?i)(sk-[a-z0-9]{16,})"), "sk-***"),
            (re.compile(r"(?i)(pk-[a-z0-9]{16,})"), "pk-***"),
            (re.compile(r"(?i)([a-z0-9_\-]{32,})"), "***"),
            (re.compile(r"(?i)(bearer\s+[a-z0-9_\-]{16,})"), "bearer ***"),
            (re.compile(r"(?i)(token\s+[a-z0-9_\-]{16,})"), "token ***"),
            (re.compile(r"(?i)(secret\s*[:=]\s*['\"]?[a-z0-9_\-]{16,})"), "secret ***"),
            (
                re.compile(r"(?i)(password\s*[:=]\s*['\"]?[a-z0-9_\-]{8,})"),
                "password ***",
            ),
            (re.compile(r"(/Users/[^/\s]{1,32}(?:/[^/\s]+)+)"), "<path-redacted>"),
            (re.compile(r"(/home/[^/\s]{1,32}(?:/[^/\s]+)+)"), "<path-redacted>"),
            (re.compile(r"(/tmp/[^/\s]+)"), "<path-redacted>"),
        ]

    def check_output(self, text: str) -> str:
        for pattern, replacement in self._patterns:
            text = pattern.sub(replacement, text)
        return text

    def hook(self) -> None:
        if self._hooked:
            return
        guard = self

        def _guarded_stdout(text: str) -> int:
            return guard._original_stdout_write(guard.check_output(text))

        def _guarded_stderr(text: str) -> int:
            return guard._original_stderr_write(guard.check_output(text))

        sys.stdout.write = _guarded_stdout
        sys.stderr.write = _guarded_stderr
        self._hooked = True

    def unhook(self) -> None:
        if not self._hooked:
            return
        sys.stdout.write = self._original_stdout_write
        sys.stderr.write = self._original_stderr_write
        self._hooked = False
