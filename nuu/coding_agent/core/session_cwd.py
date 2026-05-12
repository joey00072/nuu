"""
Current working directory tracking for sessions. Handles session-to-cwd
mapping and path normalization for workspace-relative operations.

Owns: session CWD resolution logic.
Delegates to: os, pathlib for path operations.

Depends on: standard library only (os, pathlib, dataclasses)
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class SessionCwdIssue:
    session_cwd: str
    fallback_cwd: str
    session_file: str | None = None


class MissingSessionCwdError(Exception):
    def __init__(self, issue: SessionCwdIssue) -> None:
        self.issue = issue
        super().__init__(self._format())

    def _format(self) -> str:
        session_file = (
            f"\nSession file: {self.issue.session_file}"
            if self.issue.session_file
            else ""
        )
        return (
            f"Stored session working directory does not exist: "
            f"{self.issue.session_cwd}{session_file}\n"
            f"Current working directory: {self.issue.fallback_cwd}"
        )


class SessionCwd:
    def __init__(self, cwd: str | None = None) -> None:
        self._cwd: str = os.path.abspath(cwd) if cwd else os.getcwd()
        self._validate()

    def get(self) -> str:
        return self._cwd

    def set(self, path: str) -> None:
        resolved = os.path.abspath(path)
        if not os.path.exists(resolved):
            os.makedirs(resolved, exist_ok=True)
        if not os.path.isdir(resolved):
            raise NotADirectoryError(f"Not a directory: {resolved}")
        self._cwd = resolved

    def resolve(self, relative_path: str) -> str:
        return os.path.normpath(os.path.join(self._cwd, relative_path))

    def check_issue(self, session_file: str | None = None) -> SessionCwdIssue | None:
        if session_file and not os.path.exists(self._cwd):
            return SessionCwdIssue(
                session_cwd=self._cwd,
                fallback_cwd=os.getcwd(),
                session_file=session_file,
            )
        return None

    def _validate(self) -> None:
        if not os.path.exists(self._cwd):
            os.makedirs(self._cwd, exist_ok=True)
        if not os.path.isdir(self._cwd):
            raise NotADirectoryError(f"Not a directory: {self._cwd}")

    def __str__(self) -> str:
        return self._cwd
