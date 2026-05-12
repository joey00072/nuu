"""Login/logout picker widgets."""

from __future__ import annotations

from typing import Callable

from ..engine import ansi, theme
from ..engine.keys import decode_printable_key

OAUTH_PROVIDER_IDS = {"anthropic", "github-copilot", "openai-codex"}


def _decode_printable(data: str) -> str | None:
    return decode_printable_key(data)


def _fuzzy_match(query: str, target: str) -> bool:
    """Return True if all chars in query appear in order in target."""
    it = iter(target.lower())
    return all(c in it for c in query.lower())


class AuthTypeComponent:
    """Two-option selector for 'Use a subscription' vs 'Use an API key'."""

    def __init__(self) -> None:
        self._options = [
            ("subscription", "Use a subscription"),
            ("api_key", "Use an API key"),
        ]
        self._sel = 0
        self.on_select: Callable[[str], None] | None = None
        self.on_cancel: Callable[[], None] | None = None

    def handle_input(self, data: str) -> None:
        from ..engine.keybindings import get_keybindings
        kb = get_keybindings()
        if kb.matches(data, "tui.select.cancel") or kb.matches(data, "tui.input.copy"):
            if self.on_cancel:
                self.on_cancel()
            return
        if kb.matches(data, "tui.select.confirm"):
            if self.on_select:
                self.on_select(self._options[self._sel][0])
            return
        if kb.matches(data, "tui.select.up"):
            self._sel = (self._sel - 1) % len(self._options)
            return
        if kb.matches(data, "tui.select.down"):
            self._sel = (self._sel + 1) % len(self._options)
            return

    def render(self, width: int) -> list[str]:
        accent_hex = theme._resolve("accent")
        dim_hex = theme._resolve("dim")
        border_hex = theme._resolve("borderMuted")
        lines: list[str] = []

        lines.append(ansi.fg(dim_hex, " Select authentication method:"))
        lines.append("")

        for i, (_id, label) in enumerate(self._options):
            is_sel = i == self._sel
            prefix = ansi.fg(accent_hex, "\u2192 ") if is_sel else "  "
            text = (
                ansi.bold(ansi.fg(accent_hex, label))
                if is_sel
                else ansi.fg(dim_hex, label)
            )
            lines.append(" " + prefix + text)

        lines.append("")
        lines.append(
            ansi.fg(dim_hex, "  (\u2191\u2193 navigate  Enter select  Esc cancel)")
        )
        lines.append(ansi.fg(border_hex, "\u2500" * width))
        return lines

    def invalidate(self) -> None:
        pass


class LoginPickerComponent:
    """Provider selector for /login and /logout commands."""

    def __init__(
        self,
        providers: list[tuple[str, str, str]],
        mode: str = "login",
    ) -> None:
        self._all = providers
        self._filtered = list(self._all)
        self._sel = 0
        self._query = ""
        self._mode = mode
        self.on_select: Callable[[str], None] | None = None
        self.on_cancel: Callable[[], None] | None = None

    def handle_input(self, data: str) -> None:
        from ..engine.keybindings import get_keybindings
        kb = get_keybindings()
        if kb.matches(data, "tui.select.cancel") or kb.matches(data, "tui.input.copy"):
            if self.on_cancel:
                self.on_cancel()
            return
        if kb.matches(data, "tui.select.confirm"):
            if self._filtered and self.on_select:
                provider_id = self._filtered[self._sel][0]
                self.on_select(provider_id)
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

    def _refilter(self) -> None:
        q = self._query.strip()
        if q:
            self._filtered = [
                p
                for p in self._all
                if _fuzzy_match(q, p[0]) or _fuzzy_match(q, p[1])
            ]
        else:
            self._filtered = list(self._all)
        self._sel = 0

    def render(self, width: int) -> list[str]:
        border_hex = theme._resolve("borderMuted")
        accent_hex = theme._resolve("accent")
        dim_hex = theme._resolve("dim")
        success_hex = theme._resolve("success")

        lines: list[str] = []
        inner_w = max(10, width - 4)

        title_text = " login " if self._mode == "login" else " logout "
        title = f"{title_text} (\u2191\u2193 navigate  Enter select  Esc cancel) "
        lines.append(ansi.fg(dim_hex, title[:width]))

        prompt = ansi.fg(accent_hex, "> ") + self._query
        search_line = " " + ansi.pad_to_width(prompt, inner_w) + " "
        lines.append(search_line)

        lines.append(ansi.fg(border_hex, "\u2500" * width))

        visible_count = min(20, len(self._filtered))
        start = max(0, self._sel - visible_count // 2)
        start = min(start, max(0, len(self._filtered) - visible_count))
        end = start + visible_count

        for i in range(start, end):
            pid, pname, status = self._filtered[i]
            is_sel = i == self._sel
            prefix = ansi.fg(accent_hex, "\u2192 ") if is_sel else "  "

            if status == "configured":
                status_label = ansi.fg(success_hex, "\u2713 configured")
            elif status == "env":
                status_label = ansi.fg(dim_hex, "env")
            else:
                status_label = ansi.fg(dim_hex, "unconfigured")

            label = f"{pname}  {status_label}"
            if is_sel:
                label = ansi.bold(ansi.fg(accent_hex, label))
            else:
                label = ansi.fg(dim_hex, label)
            row = prefix + ansi.truncate_to_width(label, inner_w)
            lines.append(" " + row)

        lines.append(
            ansi.fg(dim_hex, f"  {len(self._filtered)} / {len(self._all)} providers")
        )
        lines.append(ansi.fg(border_hex, "\u2500" * width))

        return lines

    def invalidate(self) -> None:
        pass
