"""
Slash command definitions and routing for the coding agent's interactive mode.
Defines available commands, argument schemas, and handler signatures.

Owns: slash command TypedDicts and command definitions.
Delegates to: command handler implementations.

Depends on: standard library only (typing)
"""

from __future__ import annotations

from typing import TypedDict


class BuiltinSlashCommand(TypedDict):
    name: str
    description: str


BUILTIN_SLASH_COMMANDS: list[BuiltinSlashCommand] = [
    {"name": "help", "description": "Show available commands and usage"},
    {"name": "compact", "description": "Manually compact the session context"},
    {"name": "reset", "description": "Reset the current session"},
    {"name": "undo", "description": "Undo the last action"},
    {"name": "retry", "description": "Retry the last response"},
    {"name": "history", "description": "Show session history"},
    {"name": "model", "description": "Select or switch the active model"},
    {"name": "thinking", "description": "Toggle thinking mode"},
    {"name": "exit", "description": "Exit the application"},
]


def find_command(name: str) -> BuiltinSlashCommand | None:
    for cmd in BUILTIN_SLASH_COMMANDS:
        if cmd["name"] == name:
            return cmd
    return None
