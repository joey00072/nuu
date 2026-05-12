"""Interactive settings picker overlay — Pi TUI style."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..engine import ansi, theme
from ..engine.keys import decode_printable_key
from ...coding_agent.core.settings_manager import SettingsManager


@dataclass
class _SettingItem:
    key: str
    label: str
    kind: str  # "bool" or "enum"
    value: Any = None
    options: list[Any] = field(default_factory=list)


_SETTINGS_DEFS: list[tuple[str, str, str, list[Any]]] = [
    ("auto_compact", "Auto-compact", "bool", []),
    ("steering_mode", "Steering mode", "enum", ["one-at-a-time", "all"]),
    ("followup_mode", "Follow-up mode", "enum", ["one-at-a-time", "all"]),
    (
        "transport",
        "Transport",
        "enum",
        ["sse", "websocket", "websocket-cached", "auto"],
    ),
    ("hide_thinking", "Hide thinking", "bool", []),
    ("collapse_changelog", "Collapse changelog", "bool", []),
    ("default_provider", "Default provider", "str", []),
    ("default_model", "Default model", "str", []),
]


def _read_value(sm: SettingsManager, key: str) -> Any:
    if key == "auto_compact":
        comp = sm.get_all().get("compaction", {})
        if isinstance(comp, dict):
            return comp.get("enabled", True)
        return getattr(comp, "enabled", True)
    return sm.get(key, "")


def _apply_value(sm: SettingsManager, key: str, value: Any) -> None:
    if key == "auto_compact":
        comp = dict(sm.get_all().get("compaction", {}))
        comp["enabled"] = value
        sm.set("compaction", comp)
    else:
        sm.set(key, value)
    sm.save()


def _decode_printable(data: str) -> str | None:
    return decode_printable_key(data)


class SettingsPickerComponent:
    """Overlay component for interactive settings editing."""

    def __init__(self, settings_manager: SettingsManager) -> None:
        self._sm = settings_manager
        self._sel = 0
        self._query = ""
        self._items: list[_SettingItem] = []
        self._filtered: list[_SettingItem] = []
        self._rebuild()
        self.on_select: Callable[[str, Any], None] | None = None
        self.on_cancel: Callable[[], None] | None = None

    def _rebuild(self) -> None:
        self._items = []
        for key, label, kind, options in _SETTINGS_DEFS:
            val = _read_value(self._sm, key)
            self._items.append(
                _SettingItem(
                    key=key, label=label, kind=kind, value=val, options=options
                )
            )
        self._refilter()

    def _refilter(self) -> None:
        q = self._query.lower()
        if q:
            self._filtered = [
                it for it in self._items if q in it.label.lower() or q in it.key.lower()
            ]
        else:
            self._filtered = list(self._items)
        self._sel = 0

    def handle_input(self, data: str) -> None:
        from ..engine.keybindings import get_keybindings

        kb = get_keybindings()
        if kb.matches(data, "tui.select.cancel") or kb.matches(data, "tui.input.copy"):
            if self.on_cancel:
                self.on_cancel()
            return
        if kb.matches(data, "tui.select.confirm"):
            if not self._filtered:
                if self.on_cancel:
                    self.on_cancel()
                return
            item = self._filtered[self._sel]
            if item.kind == "bool":
                item.value = not item.value
                _apply_value(self._sm, item.key, item.value)
                if self.on_select:
                    self.on_select(item.key, item.value)
            elif item.kind == "enum" and item.options:
                idx = (
                    item.options.index(item.value) if item.value in item.options else 0
                )
                item.value = item.options[(idx + 1) % len(item.options)]
                _apply_value(self._sm, item.key, item.value)
                if self.on_select:
                    self.on_select(item.key, item.value)
            return
        if kb.matches(data, "tui.select.up"):
            if self._filtered:
                self._sel = (self._sel - 1) % len(self._filtered)
            return
        if kb.matches(data, "tui.select.down"):
            if self._filtered:
                self._sel = (self._sel + 1) % len(self._filtered)
            return
        if kb.matches(data, "tui.editor.deleteCharBackward"):
            self._query = self._query[:-1]
            self._refilter()
            return
        printable = _decode_printable(data)
        if printable and printable.isprintable():
            self._query += printable
            self._refilter()
            return

    def render(self, width: int) -> list[str]:
        border_hex = theme._resolve("borderMuted")
        accent_hex = theme._resolve("accent")
        dim_hex = theme._resolve("dim")

        lines: list[str] = []
        inner_w = max(10, width - 4)

        title = " settings  (↑↓ navigate  Enter toggle  Esc close) "
        lines.append(ansi.fg(dim_hex, title[:width]))

        prompt = ansi.fg(accent_hex, "> ") + self._query
        search_line = " " + ansi.pad_to_width(prompt, inner_w) + " "
        lines.append(search_line)

        lines.append(ansi.fg(border_hex, "─" * width))

        visible_count = min(12, len(self._filtered))
        start = max(0, self._sel - visible_count // 2)
        start = min(start, max(0, len(self._filtered) - visible_count))
        end = start + visible_count

        for i in range(start, end):
            item = self._filtered[i]
            is_sel = i == self._sel

            prefix = ansi.fg(accent_hex, "→ ") if is_sel else "  "
            label_colon = ansi.truncate_to_width(item.label + " ", 30)

            if item.kind == "bool":
                val_str = ansi.fg(
                    accent_hex if item.value else dim_hex, str(item.value).lower()
                )
            elif item.kind == "enum":
                val_str = ansi.fg(accent_hex, str(item.value))
            else:
                val_str = ansi.fg(dim_hex, str(item.value) if item.value else "(none)")

            row = prefix + label_colon + val_str
            if is_sel:
                row = ansi.bold(row)
            lines.append(" " + ansi.truncate_to_width(row, inner_w))

        lines.append(ansi.fg(border_hex, "─" * width))
        return lines

    def invalidate(self) -> None:
        self._rebuild()
