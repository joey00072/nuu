"""
Built-in slash command definitions for the coding agent CLI. Defines the
available commands, their arguments, and descriptions.

Owns: BuiltinSlashCommand model, built-in command list.
Delegates to: pydantic for model validation.

Depends on: pydantic
"""

from typing import List
from pydantic import BaseModel


class BuiltinSlashCommand(BaseModel):
    name: str
    description: str


BUILTIN_SLASH_COMMANDS: List[BuiltinSlashCommand] = [
    BuiltinSlashCommand(name="settings", description="Open settings menu"),
    BuiltinSlashCommand(name="model", description="Select model (opens selector UI)"),
    BuiltinSlashCommand(
        name="scoped-models", description="Enable/disable models for cycling"
    ),
    BuiltinSlashCommand(
        name="export",
        description="Export session (HTML default, or specify path: .html/.jsonl)",
    ),
    BuiltinSlashCommand(
        name="import", description="Import and resume a session from a JSONL file"
    ),
    BuiltinSlashCommand(
        name="share", description="Share session as a secret GitHub gist"
    ),
    BuiltinSlashCommand(
        name="copy", description="Copy last agent message to clipboard"
    ),
    BuiltinSlashCommand(name="name", description="Set session display name"),
    BuiltinSlashCommand(name="session", description="Show session info and stats"),
    BuiltinSlashCommand(name="changelog", description="Show changelog entries"),
    BuiltinSlashCommand(name="hotkeys", description="Show all keyboard shortcuts"),
    BuiltinSlashCommand(name="fork", description="Fork from a previous user message"),
    BuiltinSlashCommand(name="clone", description="Duplicate the current session"),
    BuiltinSlashCommand(
        name="tree", description="Navigate session tree (switch branches)"
    ),
    BuiltinSlashCommand(name="login", description="Configure provider authentication"),
    BuiltinSlashCommand(name="logout", description="Remove provider authentication"),
    BuiltinSlashCommand(name="new", description="Start a new session"),
    BuiltinSlashCommand(
        name="compact", description="Manually compact the session context"
    ),
    BuiltinSlashCommand(name="resume", description="Resume a different session"),
    BuiltinSlashCommand(
        name="reload",
        description="Reload keybindings, extensions, skills, prompts, and themes",
    ),
    BuiltinSlashCommand(name="debug", description="Print debug information"),
    BuiltinSlashCommand(name="quit", description="Quit nuu"),
]
