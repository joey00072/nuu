"""Scoped-models selector — enable/disable/reorder models for Ctrl+P cycling."""

from __future__ import annotations

from typing import Callable

from ..engine import ansi, theme
from ..engine.keys import decode_printable_key
from ...ai.types import ModelInfo


# ---------------------------------------------------------------------------
# Enabled-set helpers (mirrors Pi's scoped-models-selector.ts logic)
# ---------------------------------------------------------------------------

EnabledIds = list[str] | None  # None = all enabled


def _is_enabled(enabled: EnabledIds, full_id: str) -> bool:
    return enabled is None or full_id in enabled


def _toggle(enabled: EnabledIds, all_ids: list[str], full_id: str) -> EnabledIds:
    if enabled is None:
        # First toggle: start with only this one
        return [full_id]
    if full_id in enabled:
        result = [i for i in enabled if i != full_id]
        return result
    return enabled + [full_id]


def _enable_all(enabled: EnabledIds, all_ids: list[str], targets: list[str] | None = None) -> EnabledIds:
    if enabled is None:
        return None
    to_add = targets if targets is not None else all_ids
    result = list(enabled)
    for i in to_add:
        if i not in result:
            result.append(i)
    return None if len(result) == len(all_ids) else result


def _clear_all(enabled: EnabledIds, all_ids: list[str], targets: list[str] | None = None) -> EnabledIds:
    if enabled is None:
        to_remove = set(targets if targets is not None else all_ids)
        result = [i for i in all_ids if i not in to_remove]
        return result
    to_remove = set(targets if targets is not None else enabled)
    return [i for i in enabled if i not in to_remove]


def _move(enabled: EnabledIds, full_id: str, delta: int) -> EnabledIds:
    if enabled is None:
        return None
    lst = list(enabled)
    idx = lst.index(full_id) if full_id in lst else -1
    if idx < 0:
        return lst
    new_idx = idx + delta
    if new_idx < 0 or new_idx >= len(lst):
        return lst
    lst[idx], lst[new_idx] = lst[new_idx], lst[idx]
    return lst


def _sorted_ids(enabled: EnabledIds, all_ids: list[str]) -> list[str]:
    if enabled is None:
        return list(all_ids)
    enabled_set = set(enabled)
    return list(enabled) + [i for i in all_ids if i not in enabled_set]


# ---------------------------------------------------------------------------
# Component
# ---------------------------------------------------------------------------


class ScopedModelsSelectorComponent:
    """Enable/disable/reorder models for Ctrl+P cycling."""

    def __init__(
        self,
        all_models: list[ModelInfo],
        enabled_model_ids: list[str] | None,
    ) -> None:
        self._models_by_id: dict[str, ModelInfo] = {}
        self._all_ids: list[str] = []
        for m in all_models:
            full_id = f"{m.provider}/{m.id}"
            self._models_by_id[full_id] = m
            self._all_ids.append(full_id)

        self._enabled: EnabledIds = list(enabled_model_ids) if enabled_model_ids is not None else None
        self._dirty = False

        self._query = ""
        self._cursor = 0
        self._sel = 0
        self._filtered: list[str] = []
        self._refilter()

        self.on_change: Callable[[list[str] | None], None] | None = None
        self.on_save: Callable[[list[str] | None], None] | None = None
        self.on_cancel: Callable[[], None] | None = None

    # ------------------------------------------------------------------

    def _refilter(self) -> None:
        ordered = _sorted_ids(self._enabled, self._all_ids)
        if self._query:
            q = self._query.lower()
            scored = [(s, i) for i in ordered if i in self._models_by_id and (s := self._match_score(i, q)) is not None]
            scored.sort(key=lambda x: -x[0])
            ordered = [i for _, i in scored]
        self._filtered = [i for i in ordered if i in self._models_by_id]
        self._sel = min(self._sel, max(0, len(self._filtered) - 1))

    def _fuzzy_score(self, text: str, query: str) -> int | None:
        ti = 0
        qi = 0
        score = 0
        consecutive = 0
        while ti < len(text) and qi < len(query):
            if text[ti] == query[qi]:
                consecutive += 1
                score += consecutive * 2
                if ti == 0 or text[ti - 1] in "-_/.@ ":
                    score += 3
                qi += 1
            else:
                consecutive = 0
            ti += 1
        return score if qi == len(query) else None

    def _match_score(self, full_id: str, q: str) -> int | None:
        m = self._models_by_id.get(full_id)
        if not m:
            return None
        scores = [
            self._fuzzy_score(m.id.lower(), q),
            self._fuzzy_score(m.provider.lower(), q),
            self._fuzzy_score(full_id.lower(), q),
        ]
        valid = [s for s in scores if s is not None]
        return max(valid) if valid else None

    def _matches(self, full_id: str, q: str) -> bool:
        return self._match_score(full_id, q) is not None

    def _insert(self, text: str) -> None:
        self._query = self._query[: self._cursor] + text + self._query[self._cursor :]
        self._cursor += len(text)
        self._sel = 0
        self._refilter()

    # ------------------------------------------------------------------

    def handle_input(self, data: str) -> None:
        from ..engine.keybindings import get_keybindings

        kb = get_keybindings()

        # Cancel
        if kb.matches(data, "tui.select.cancel"):
            if self._query:
                self._query = ""
                self._cursor = 0
                self._sel = 0
                self._refilter()
            elif self.on_cancel:
                self.on_cancel()
            return

        # Navigation
        if kb.matches(data, "tui.select.up"):
            if self._filtered:
                self._sel = (self._sel - 1) % len(self._filtered)
            return
        if kb.matches(data, "tui.select.down"):
            if self._filtered:
                self._sel = (self._sel + 1) % len(self._filtered)
            return

        # Toggle selected item on Enter
        if kb.matches(data, "tui.select.confirm"):
            item = self._filtered[self._sel] if self._filtered else None
            if item:
                self._enabled = _toggle(self._enabled, self._all_ids, item)
                self._dirty = True
                self._refilter()
                if self.on_change:
                    self.on_change(list(self._enabled) if self._enabled is not None else None)
            return

        # Ctrl+A — enable all (filtered)
        if data == "\x01":
            targets = self._filtered if self._query else None
            self._enabled = _enable_all(self._enabled, self._all_ids, targets)
            self._dirty = True
            self._refilter()
            if self.on_change:
                self.on_change(list(self._enabled) if self._enabled is not None else None)
            return

        # Ctrl+X — clear all (filtered)
        if data == "\x18":
            targets = self._filtered if self._query else None
            self._enabled = _clear_all(self._enabled, self._all_ids, targets)
            self._dirty = True
            self._refilter()
            if self.on_change:
                self.on_change(list(self._enabled) if self._enabled is not None else None)
            return

        # Ctrl+P — toggle entire provider
        if data == "\x10":
            item = self._filtered[self._sel] if self._filtered else None
            if item:
                m = self._models_by_id.get(item)
                if m:
                    provider_ids = [i for i in self._all_ids if self._models_by_id.get(i) and self._models_by_id[i].provider == m.provider]
                    all_on = all(_is_enabled(self._enabled, i) for i in provider_ids)
                    if all_on:
                        self._enabled = _clear_all(self._enabled, self._all_ids, provider_ids)
                    else:
                        self._enabled = _enable_all(self._enabled, self._all_ids, provider_ids)
                    self._dirty = True
                    self._refilter()
                    if self.on_change:
                        self.on_change(list(self._enabled) if self._enabled is not None else None)
            return

        # Ctrl+S — save
        if data == "\x13":
            if self.on_save:
                self.on_save(list(self._enabled) if self._enabled is not None else None)
            self._dirty = False
            return

        # Alt+Up — reorder up
        if kb.matches(data, "app.models.reorderUp"):
            item = self._filtered[self._sel] if self._filtered else None
            if item and self._enabled is not None and item in self._enabled:
                new_enabled = _move(self._enabled, item, -1)
                if new_enabled != self._enabled:
                    self._enabled = new_enabled
                    self._dirty = True
                    self._sel = max(0, self._sel - 1)
                    self._refilter()
                    if self.on_change:
                        self.on_change(list(self._enabled) if self._enabled is not None else None)
            return

        # Alt+Down — reorder down
        if kb.matches(data, "app.models.reorderDown"):
            item = self._filtered[self._sel] if self._filtered else None
            if item and self._enabled is not None and item in self._enabled:
                new_enabled = _move(self._enabled, item, 1)
                if new_enabled != self._enabled:
                    self._enabled = new_enabled
                    self._dirty = True
                    self._sel = min(len(self._filtered) - 1, self._sel + 1)
                    self._refilter()
                    if self.on_change:
                        self.on_change(list(self._enabled) if self._enabled is not None else None)
            return

        # Search box edits
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
        if kb.matches(data, "tui.editor.deleteCharBackward"):
            if self._cursor > 0:
                self._query = self._query[: self._cursor - 1] + self._query[self._cursor :]
                self._cursor -= 1
                self._sel = 0
                self._refilter()
            return
        if kb.matches(data, "tui.editor.deleteCharForward"):
            if self._cursor < len(self._query):
                self._query = self._query[: self._cursor] + self._query[self._cursor + 1 :]
                self._sel = 0
                self._refilter()
            return
        if kb.matches(data, "tui.editor.deleteToLineStart"):
            self._query = self._query[self._cursor :]
            self._cursor = 0
            self._sel = 0
            self._refilter()
            return
        if kb.matches(data, "tui.editor.deleteToLineEnd"):
            self._query = self._query[: self._cursor]
            self._sel = 0
            self._refilter()
            return

        # Printable input → search
        printable = decode_printable_key(data)
        if printable is None and data and not data.startswith("\x1b") and all(32 <= ord(c) for c in data):
            printable = data
        if printable and printable.isprintable():
            self._insert(printable)

    # ------------------------------------------------------------------

    def render(self, width: int) -> list[str]:
        accent = theme._resolve("accent")
        dim = theme._resolve("dim")
        border = theme._resolve("borderMuted")
        success = theme._resolve("success")
        warning = theme._resolve("warning")

        lines: list[str] = []
        inner_w = max(10, width - 4)

        # Title
        title = " models  (↑↓ nav  Enter toggle  ^A all  ^X clear  ^P provider  ^S save  Esc cancel) "
        lines.append(ansi.fg(dim, title[:width]))

        # Search box
        before = self._query[: self._cursor]
        at = self._query[self._cursor] if self._cursor < len(self._query) else " "
        after = self._query[self._cursor + 1 :] if self._cursor < len(self._query) else ""
        prompt = ansi.fg(accent, "> ") + before + ansi.reverse(at) + after
        lines.append(" " + ansi.pad_to_width(prompt, inner_w) + " ")

        # Separator
        lines.append(ansi.fg(border, "─" * width))

        # Model list
        all_enabled = self._enabled is None
        page_size = min(10, len(self._filtered))
        start = max(0, self._sel - page_size // 2)
        start = min(start, max(0, len(self._filtered) - page_size))
        end = start + page_size

        for i in range(start, end):
            full_id = self._filtered[i]
            m = self._models_by_id.get(full_id)
            if not m:
                continue
            is_sel = i == self._sel
            enabled = _is_enabled(self._enabled, full_id)

            prefix = ansi.fg(accent, "→ ") if is_sel else "  "
            model_text = ansi.bold(ansi.fg(accent, m.id)) if is_sel else m.id
            badge = ansi.fg(dim, f" [{m.provider}]")

            if all_enabled:
                status = ""
            elif enabled:
                status = ansi.fg(success, " ✓")
            else:
                status = ansi.fg(dim, " ✗")

            label = model_text + badge + status
            row = prefix + ansi.truncate_to_width(label, inner_w)
            lines.append(" " + row)

        if not self._filtered:
            lines.append(ansi.fg(dim, "  No matching models"))

        # Scroll indicator + count
        if len(self._filtered) > 0:
            selected_full = self._filtered[self._sel] if self._sel < len(self._filtered) else None
            if selected_full:
                m = self._models_by_id.get(selected_full)
                if m:
                    lines.append(ansi.fg(dim, f"  {m.name}"))

        scroll = (
            f" ({self._sel + 1}/{len(self._filtered)})"
            if page_size < len(self._filtered)
            else ""
        )
        enabled_count = len(self._enabled) if self._enabled is not None else len(self._all_ids)
        count_label = "all enabled" if self._enabled is None else f"{enabled_count}/{len(self._all_ids)} enabled"
        footer = f"  {len(self._filtered)} shown · {count_label}{scroll}"
        if self._dirty:
            footer_line = ansi.fg(dim, footer) + " " + ansi.fg(warning, "(unsaved — ^S to save)")
        else:
            footer_line = ansi.fg(dim, footer)
        lines.append(footer_line)

        lines.append(ansi.fg(border, "─" * width))
        return lines

    def invalidate(self) -> None:
        pass

    @property
    def enabled_ids(self) -> list[str] | None:
        return list(self._enabled) if self._enabled is not None else None
