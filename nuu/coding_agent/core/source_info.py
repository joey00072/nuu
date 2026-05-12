"""
Source information data models. Tracks where messages originate (user, tool,
system) and associated metadata for provenance.

Owns: source info dataclasses.
Delegates to: nothing (data models only).

Depends on: standard library only (dataclasses, typing)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceInfo:
    type: str
    path: str
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)


def create_skill_source(path: str, description: str = "") -> SourceInfo:
    return SourceInfo(
        type="skill",
        path=path,
        description=description,
        metadata={"source": "skill"},
    )


def create_config_source(path: str, description: str = "") -> SourceInfo:
    return SourceInfo(
        type="config",
        path=path,
        description=description,
        metadata={"source": "config"},
    )


def create_context_file_source(path: str, description: str = "") -> SourceInfo:
    return SourceInfo(
        type="context-file",
        path=path,
        description=description,
        metadata={"source": "context-file"},
    )
