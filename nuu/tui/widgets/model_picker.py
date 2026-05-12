"""Model picker overlay — Pi TUI style."""

from __future__ import annotations
from typing import Callable
from ..engine import ansi, theme
from ..engine.keys import decode_printable_key
from ...ai.models import get_models, get_providers
from ...ai.types import ModelInfo


def _decode_printable(data: str) -> str | None:
    return decode_printable_key(data)


def _configured_providers() -> set[str]:
    from ...coding_agent.session import _resolve_api_key

    return {p for p in get_providers() if _resolve_api_key(p)}


def _all_models() -> list[ModelInfo]:
    return [m for p in get_providers() for m in get_models(p)]


def _fuzzy_score(text: str, query: str) -> int | None:
    """Subsequence match score. None = no match. Higher = better."""
    ti = 0
    qi = 0
    score = 0
    consecutive = 0
    while ti < len(text) and qi < len(query):
        if text[ti] == query[qi]:
            consecutive += 1
            score += consecutive * 2
            if ti == 0 or text[ti - 1] in "-_/.@ ":
                score += 3  # word boundary bonus
            qi += 1
        else:
            consecutive = 0
        ti += 1
    return score if qi == len(query) else None


def _match_score(m: ModelInfo, q: str) -> int | None:
    q = q.lower()
    scores = [
        _fuzzy_score(m.id.lower(), q),
        _fuzzy_score(m.provider.lower(), q),
        _fuzzy_score(f"{m.provider}/{m.id}".lower(), q),
    ]
    valid = [s for s in scores if s is not None]
    return max(valid) if valid else None


class ModelPickerComponent:
    """Overlay component for model selection."""

    def __init__(self, current: ModelInfo | None = None, initial_query: str = "") -> None:
        self._current = current
        self._configured = _configured_providers()
        self._all = _all_models()
        self._all.sort(
            key=lambda m: (
                0
                if self._current
                and m.id == self._current.id
                and m.provider == self._current.provider
                else 1,
                0 if m.provider in self._configured else 1,
                m.provider,
                m.id,
            )
        )
        self._filtered = list(self._all)
        self._sel = 0
        self._query = initial_query
        self._cursor = len(initial_query)
        if initial_query:
            self._refilter()
        self.on_select: "Callable[[ModelInfo], None] | None" = None
        self.on_cancel: "Callable[[], None] | None" = None

    def _insert(self, text: str) -> None:
        self._query = self._query[: self._cursor] + text + self._query[self._cursor :]
        self._cursor += len(text)
        self._refilter()

    def handle_input(self, data: str) -> None:
        from ..engine.keybindings import get_keybindings

        kb = get_keybindings()

        if kb.matches(data, "tui.select.cancel"):
            if self.on_cancel:
                self.on_cancel()
            return
        if kb.matches(data, "tui.select.confirm"):
            if self._filtered and self.on_select:
                self.on_select(self._filtered[self._sel])
            return
        if kb.matches(data, "tui.select.up"):
            if self._filtered:
                self._sel = (self._sel - 1) % len(self._filtered)
            return
        if kb.matches(data, "tui.select.down"):
            if self._filtered:
                self._sel = (self._sel + 1) % len(self._filtered)
            return

        # Cursor movement within query
        if kb.matches(data, "tui.editor.cursorLeft"):
            self._cursor = max(0, self._cursor - 1)
            return
        if kb.matches(data, "tui.editor.cursorRight"):
            self._cursor = min(len(self._query), self._cursor + 1)
            return
        if kb.matches(data, "tui.editor.cursorLineStart"):
            self._cursor = 0
            return
        if kb.matches(data, "tui.editor.cursorLineEnd"):
            self._cursor = len(self._query)
            return

        # Deletion
        if kb.matches(data, "tui.editor.deleteCharBackward"):
            if self._cursor > 0:
                self._query = self._query[: self._cursor - 1] + self._query[self._cursor :]
                self._cursor -= 1
                self._refilter()
            return
        if kb.matches(data, "tui.editor.deleteCharForward"):
            if self._cursor < len(self._query):
                self._query = self._query[: self._cursor] + self._query[self._cursor + 1 :]
                self._refilter()
            return
        if kb.matches(data, "tui.editor.deleteToLineStart"):
            self._query = self._query[self._cursor :]
            self._cursor = 0
            self._refilter()
            return
        if kb.matches(data, "tui.editor.deleteToLineEnd"):
            self._query = self._query[: self._cursor]
            self._refilter()
            return

        # Printable character
        printable = _decode_printable(data)
        if printable is None and data and not data.startswith("\x1b") and all(32 <= ord(c) for c in data):
            printable = data
        if printable and printable.isprintable():
            self._insert(printable)
            return

    def _refilter(self) -> None:
        if self._query:
            scored = [(s, m) for m in self._all if (s := _match_score(m, self._query)) is not None]
            scored.sort(key=lambda x: -x[0])
            self._filtered = [m for _, m in scored]
        else:
            self._filtered = list(self._all)
        self._sel = 0

    def render(self, width: int) -> list[str]:
        border_hex = theme._resolve("borderMuted")
        accent_hex = theme._resolve("accent")
        dim_hex = theme._resolve("dim")
        warning_hex = theme._resolve("warning")

        lines: list[str] = []
        inner_w = max(10, width - 4)

        # Title
        title = " model  (↑↓ navigate  type to search  Enter select  Esc cancel) "
        lines.append(ansi.fg(dim_hex, title[:width]))

        # Search bar with cursor
        before = self._query[: self._cursor]
        at = self._query[self._cursor] if self._cursor < len(self._query) else " "
        after = self._query[self._cursor + 1 :] if self._cursor < len(self._query) else ""
        prompt = ansi.fg(accent_hex, "> ") + before + ansi.reverse(at) + after
        search_line = " " + ansi.pad_to_width(prompt, inner_w) + " "
        lines.append(search_line)

        # Separator
        lines.append(ansi.fg(border_hex, "─" * width))

        # Model list — show up to 10 around selected (matches Pi)
        page_size = min(10, len(self._filtered))
        start = max(0, self._sel - page_size // 2)
        start = min(start, max(0, len(self._filtered) - page_size))
        end = start + page_size

        cur_id = self._current.id if self._current else None
        cur_prov = self._current.provider if self._current else None

        for i in range(start, end):
            m = self._filtered[i]
            is_cur = m.id == cur_id and m.provider == cur_prov
            is_sel = i == self._sel
            configured = m.provider in self._configured
            prefix = ansi.fg(accent_hex, "→ ") if is_sel else "  "
            check = ansi.fg(accent_hex, " ✓") if is_cur else ""
            model_text = m.id
            badge = ansi.fg(dim_hex, f" [{m.provider}]")
            if is_sel:
                model_text = ansi.bold(ansi.fg(accent_hex, model_text))
            elif is_cur:
                pass
            elif not configured:
                model_text = ansi.fg(dim_hex, model_text)
            else:
                model_text = ansi.fg(dim_hex, model_text)
            label = model_text + badge + check
            row = prefix + ansi.truncate_to_width(label, inner_w)
            lines.append(" " + row)

        if not self._filtered:
            lines.append(ansi.fg(dim_hex, "  No matching models"))

        # Count + scroll indicator
        visible_count = end - start
        scroll = (
            f" ({self._sel + 1}/{len(self._filtered)})"
            if visible_count < len(self._filtered)
            else ""
        )
        lines.append(
            ansi.fg(dim_hex, f"  {len(self._filtered)} / {len(self._all)} models{scroll}")
        )

        if not self._configured:
            lines.append(
                ansi.fg(warning_hex, "  No providers configured. Use /login to add a provider.")
            )

        lines.append(ansi.fg(border_hex, "─" * width))

        return lines

    def invalidate(self) -> None:
        pass
