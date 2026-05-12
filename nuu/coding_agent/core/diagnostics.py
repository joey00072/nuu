"""
Diagnostic event type definitions for the coding agent. Defines the structure
of diagnostic events emitted during agent execution for debugging and telemetry.

Owns: DiagnosticEvent TypedDict.
Delegates to: nothing (type definitions only).

Depends on: standard library only (typing)
"""

from __future__ import annotations

from typing import TypedDict


class ResourceCollision(TypedDict):
    resource_type: str
    name: str
    winner_path: str
    loser_path: str
    winner_source: str | None
    loser_source: str | None


class ResourceDiagnostic(TypedDict):
    type: str
    message: str
    path: str | None
    collision: ResourceCollision | None
