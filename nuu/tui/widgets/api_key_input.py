"""API key input widget."""

from __future__ import annotations

from typing import Callable

from ..engine import ansi, theme
from ..engine.keys import decode_printable_key


def _decode_printable(data: str) -> str | None:
    return decode_printable_key(data)


class ApiKeyInputComponent:
    """Simple single-line text input for API key entry."""

    def __init__(self, provider_name: str) -> None:
        self._provider_name = provider_name
        self._text = ""
        self._cursor = 0
        self.on_submit: Callable[[str], None] | None = None
        self.on_cancel: Callable[[], None] | None = None

    def handle_input(self, data: str) -> None:
        from ..engine.keybindings import get_keybindings
        kb = get_keybindings()
        if kb.matches(data, "tui.select.cancel") or kb.matches(data, "tui.input.copy"):
            if self.on_cancel:
                self.on_cancel()
            return
        if kb.matches(data, "tui.select.confirm"):
            text = self._text.strip()
            if text and self.on_submit:
                self.on_submit(text)
            return
        if kb.matches(data, "tui.editor.deleteCharBackward"):
            if self._cursor > 0:
                self._text = self._text[: self._cursor - 1] + self._text[self._cursor :]
                self._cursor -= 1
            return
        if kb.matches(data, "tui.editor.cursorLeft"):
            if self._cursor > 0:
                self._cursor -= 1
            return
        if kb.matches(data, "tui.editor.cursorRight"):
            if self._cursor < len(self._text):
                self._cursor += 1
            return
        if kb.matches(data, "tui.editor.cursorLineStart"):
            self._cursor = 0
            return
        if kb.matches(data, "tui.editor.cursorLineEnd"):
            self._cursor = len(self._text)
            return
        printable = _decode_printable(data)
        if printable and (printable.isprintable() or printable == "\t"):
            self._text = self._text[: self._cursor] + printable + self._text[self._cursor :]
            self._cursor += len(printable)

    def render(self, width: int) -> list[str]:
        border_hex = theme._resolve("borderMuted")
        accent_hex = theme._resolve("accent")
        dim_hex = theme._resolve("dim")
        lines: list[str] = []

        title = f" API key for {self._provider_name} "
        lines.append(self._render_top_border(width, title))

        prompt = ansi.fg(dim_hex, "Paste your API key and press Enter:")
        lines.append(" " + prompt)

        input_inner = max(1, width - 4)
        before = self._text[: self._cursor]
        at = self._text[self._cursor] if self._cursor < len(self._text) else " "
        after = self._text[self._cursor + 1 :] if self._cursor < len(self._text) else ""
        cursor_char = f"{ansi.REVERSE}{at}{ansi.REVERSE_OFF}"
        rendered = before + cursor_char + after
        rendered = ansi.pad_to_width(rendered, input_inner)
        lines.append(" " + ansi.fg(accent_hex, "> ") + rendered + " ")

        hint = "(Esc to cancel, Enter to confirm)"
        lines.append(" " + ansi.fg(dim_hex, hint))

        lines.append(ansi.fg(border_hex, "\u2500" * width))
        return lines

    def _render_top_border(self, width: int, title: str) -> str:
        border_hex = theme._resolve("borderMuted")
        if not title:
            return ansi.fg(border_hex, "\u2500" * width)
        title_colored = ansi.fg(border_hex, title)
        title_w = ansi.visible_width(title)
        remaining = max(0, width - title_w)
        left = remaining // 2
        right = remaining - left
        return (
            ansi.fg(border_hex, "\u2500" * left)
            + title_colored
            + ansi.fg(border_hex, "\u2500" * right)
        )

    def invalidate(self) -> None:
        pass
