"""@-mention file picker — file autocomplete overlay (Pi-style)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable
from ..engine import ansi, theme


class AtMatch:
    """A single file/directory match for @-completion."""

    def __init__(
        self,
        display: str,
        insert: str,
        is_directory: bool,
        description: str = "",
    ) -> None:
        self.display = display
        self.insert = insert
        self.is_directory = is_directory
        self.description = description


class AtPickerComponent:
    """Overlay showing file/directory suggestions when typing @."""

    def __init__(self, cwd: str = "") -> None:
        self._cwd = cwd or os.getcwd()
        self._prefix = ""
        self._matches: list[AtMatch] = []
        self._sel = 0
        self._visible = False
        self._max_visible = 10
        self.on_select: Callable[[AtMatch], None] | None = None
        self.on_cancel: Callable[[], None] | None = None

    def is_open(self) -> bool:
        return self._visible

    def update(self, prefix: str) -> None:
        """Called when the @-prefix text changes. Shows/hides as needed."""
        self._prefix = prefix
        if not prefix:
            self._visible = False
            self._matches = []
            return

        self._matches = self._find_matches(prefix)
        self._visible = len(self._matches) > 0
        self._sel = 0

    def hide(self) -> None:
        self._visible = False
        self._matches = []
        self._prefix = ""

    def move_up(self) -> None:
        if self._matches:
            self._sel = (self._sel - 1) % len(self._matches)

    def move_down(self) -> None:
        if self._matches:
            self._sel = (self._sel + 1) % len(self._matches)

    def confirm(self) -> AtMatch | None:
        if not self._matches:
            return None
        match = self._matches[self._sel]
        self.hide()
        return match

    def get_insert_with_at(self, match: AtMatch) -> str:
        """Return the insert text with @ prefix preserved for continued completion."""
        return f"@{match.insert}"

    def _find_matches(self, prefix: str) -> list[AtMatch]:
        """Find files/directories matching the given prefix."""
        matches: list[AtMatch] = []

        # Determine search directory and query
        search_dir: str
        query: str

        # Handle absolute paths
        if prefix.startswith("/"):
            search_dir = "/"
            query = prefix[1:] if len(prefix) > 1 else ""
        # Handle home directory
        elif prefix.startswith("~/"):
            home = os.path.expanduser("~")
            rest = prefix[2:]
            if "/" in rest:
                idx = rest.rfind("/")
                search_dir = os.path.join(home, rest[:idx])
                query = rest[idx + 1:]
            else:
                search_dir = home
                query = rest
        # Handle relative paths with directory components
        elif "/" in prefix:
            idx = prefix.rfind("/")
            dir_part = prefix[:idx]
            query = prefix[idx + 1:]
            if dir_part.startswith("~"):
                search_dir = os.path.expanduser(dir_part)
            else:
                search_dir = os.path.join(self._cwd, dir_part)
        else:
            # Just a filename prefix — search in cwd
            search_dir = self._cwd
            query = prefix

        try:
            path = Path(search_dir)
            if not path.is_dir():
                return []

            entries = sorted(
                path.iterdir(),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )

            for entry in entries:
                name = entry.name
                # Skip hidden files unless query starts with .
                if not query.startswith(".") and name.startswith("."):
                    continue

                if not name.lower().startswith(query.lower()):
                    continue

                is_dir = entry.is_dir()
                # Build the insert text
                if prefix.startswith("/"):
                    insert = f"/{name}"
                elif prefix.startswith("~/"):
                    rest = prefix[2:]
                    if "/" in rest:
                        base = rest[: rest.rfind("/") + 1]
                        insert = f"~/{base}{name}"
                    else:
                        insert = f"~/{name}"
                elif "/" in prefix:
                    base = prefix[: prefix.rfind("/") + 1]
                    if base.startswith("~"):
                        insert = f"{base}{name}"
                    else:
                        insert = f"{base}{name}"
                else:
                    insert = name

                if is_dir:
                    insert += "/"

                display_name = name + ("/" if is_dir else "")
                description = str(entry.resolve()) if not is_dir else ""

                matches.append(AtMatch(
                    display=display_name,
                    insert=insert,
                    is_directory=is_dir,
                    description=description,
                ))

            # Limit results
            matches = matches[:50]

        except (PermissionError, FileNotFoundError, OSError):
            return []

        return matches

    def render(self, width: int) -> list[str]:
        if not self._visible or not self._matches:
            return []

        border_hex = theme._resolve("borderMuted")
        accent_hex = theme._resolve("accent")
        selected_bg = theme._resolve("selectedBg")
        lines: list[str] = []

        lines.append(ansi.fg(border_hex, "─" * width))
        header = theme.fg("dim", "  @ file  ")
        lines.append(ansi.truncate_to_width(header, width))
        lines.append(ansi.fg(border_hex, "─" * width))

        visible = self._matches[: self._max_visible]
        for i, match in enumerate(visible):
            selected = i == self._sel
            cursor = ansi.fg(accent_hex, "› ") if selected else "  "

            if match.is_directory:
                name = theme.fg("accent", match.display)
            else:
                name = match.display

            desc = ""
            if match.description and selected:
                desc = theme.fg("dim", f"  {match.description}")

            row = cursor + name + desc
            if selected:
                row = ansi.bg(selected_bg, ansi.pad_to_width(row, width))

            lines.append(ansi.truncate_to_width(row, width))

        if len(self._matches) > self._max_visible:
            more = theme.fg("dim", f"  ... {len(self._matches) - self._max_visible} more")
            lines.append(ansi.truncate_to_width(more, width))

        lines.append(ansi.fg(border_hex, "─" * width))
        count = theme.fg("dim", f"  ({self._sel + 1}/{len(self._matches)})")
        lines.append(ansi.truncate_to_width(count, width))

        return lines

    def invalidate(self) -> None:
        pass
