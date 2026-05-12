"""
Interactive session picker UI. Lists available sessions from the sessions
directory and lets users select one to resume.

Owns: session listing, interactive selection logic.
Delegates to: os, pathlib for session file discovery.

Data flow: sessions_dir -> list sessions -> user selection -> session_id

Depends on: standard library only (os, pathlib, typing)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

try:
    from rich.console import Console
    from rich.prompt import Prompt
    from rich.table import Table

    _rich = True
except ImportError:
    _rich = False


class SessionInfo(NamedTuple):
    path: str
    session_id: str
    timestamp: str
    preview: str


def _get_session_dir() -> Path:
    return Path.home() / ".nuu" / "sessions"


def _discover_session_files(session_dir: Path | None = None) -> list[SessionInfo]:
    sdir = session_dir or _get_session_dir()
    if not sdir.exists():
        return []

    sessions: list[SessionInfo] = []
    for f in sorted(sdir.iterdir(), key=os.path.getmtime, reverse=True):
        if f.suffix == ".jsonl":
            try:
                first_line = f.read_text("utf-8").splitlines()[0]
            except Exception:
                first_line = ""
            sessions.append(
                SessionInfo(
                    path=str(f),
                    session_id=f.stem.split("_")[-1] if "_" in f.stem else f.stem,
                    timestamp=f.stem.split("_")[0] if "_" in f.stem else "",
                    preview=first_line[:80] if first_line else "(empty)",
                )
            )

    return sessions


def pick_session(sessions: list[SessionInfo]) -> str | None:
    if not sessions:
        print("No sessions found")
        return None

    if _rich:
        console = Console()
        table = Table(title="Sessions")
        table.add_column("#", style="cyan")
        table.add_column("ID", style="green")
        table.add_column("Timestamp", style="yellow")
        table.add_column("Preview", style="dim")
        for i, s in enumerate(sessions, start=1):
            table.add_row(str(i), s.session_id, s.timestamp, s.preview[:60])
        console.print(table)

        choice = Prompt.ask(
            f"Select session (1-{len(sessions)}) or Enter to skip",
            default="",
        )
    else:
        print("\nRecent sessions:")
        for i, s in enumerate(sessions, start=1):
            print(f"  {i}. [{s.timestamp}] {s.session_id}")
        choice = input(f"\nSelect session (1-{len(sessions)}) or Enter to skip: ")

    if not choice:
        return None

    try:
        index = int(choice) - 1
        if 0 <= index < len(sessions):
            return sessions[index].path
    except (ValueError, IndexError):
        pass

    print("Invalid selection")
    return None


def pick_recent_session() -> str | None:
    sessions = _discover_session_files()
    return pick_session(sessions)
