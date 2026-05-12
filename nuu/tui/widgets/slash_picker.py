"""Slash command completion overlay — Pi TUI style component."""

from __future__ import annotations
from ..engine import ansi, theme


class SlashPickerComponent:
    """Rendered as an overlay above the editor."""

    def __init__(self, commands: list[tuple[str, str]]) -> None:
        self._all = commands
        self._filtered: list[tuple[str, str]] = []
        self._sel = 0

    def is_open(self) -> bool:
        return bool(self._filtered)

    def filter_commands(self, prefix: str) -> list[tuple[str, str]]:
        p = prefix.lower()
        return [(n, d) for n, d in self._all if n.startswith(p)]

    def show(self, matches: list[tuple[str, str]], sel: int = 0) -> None:
        self._filtered = matches
        self._sel = max(0, min(sel, len(matches) - 1)) if matches else 0

    def hide(self) -> None:
        self._filtered = []
        self._sel = 0

    def move_up(self) -> None:
        if self._filtered:
            self._sel = (self._sel - 1) % len(self._filtered)

    def move_down(self) -> None:
        if self._filtered:
            self._sel = (self._sel + 1) % len(self._filtered)

    def confirm(self) -> str | None:
        """Return selected command name, or None."""
        if not self._filtered:
            return None
        name, _ = self._filtered[self._sel]
        self.hide()
        return name

    def render(self, width: int) -> list[str]:
        if not self._filtered:
            return []

        max_show = min(10, len(self._filtered))
        items = self._filtered[:max_show]
        lines: list[str] = []

        # Top border
        border_hex = theme._resolve("borderMuted")
        lines.append(ansi.fg(border_hex, "─" * width))

        for i, (name, desc) in enumerate(items):
            selected = i == self._sel
            cmd_str = f"/{name}"
            if selected:
                cmd_part = theme.bold(theme.fg("accent", cmd_str))
                desc_part = theme.fg("accent", f"  {desc}") if desc else ""
                row = cmd_part + desc_part
            else:
                cmd_part = theme.fg("dim", cmd_str)
                desc_part = theme.fg("dim", f"  {desc}") if desc else ""
                row = cmd_part + desc_part
            lines.append(" " + ansi.truncate_to_width(row, width - 2))

        return lines

    def invalidate(self) -> None:
        pass
