"""
Pi-style TUI engine with differential rendering and overlays.

Ported from Pi's ref/pi/packages/tui/src/tui.ts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Callable, Literal

from .component import Component, Focusable
from .terminal import ProcessTerminal, Terminal
from . import ansi

log = logging.getLogger("nuu.tui")

CURSOR_MARKER = "\x1b_pi:c\x07"

OverlayAnchor = Literal[
    "center",
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
    "top-center",
    "bottom-center",
    "left-center",
    "right-center",
]

SizeValue = int | str


class OverlayMargin:
    def __init__(
        self,
        top: int = 0,
        right: int = 0,
        bottom: int = 0,
        left: int = 0,
    ) -> None:
        self.top = top
        self.right = right
        self.bottom = bottom
        self.left = left


class OverlayOptions:
    def __init__(
        self,
        width: SizeValue | None = None,
        min_width: int | None = None,
        max_height: SizeValue | None = None,
        anchor: OverlayAnchor = "center",
        offset_x: int = 0,
        offset_y: int = 0,
        row: SizeValue | None = None,
        col: SizeValue | None = None,
        margin: OverlayMargin | int | None = None,
        visible: Callable[[int, int], bool] | None = None,
        non_capturing: bool = False,
    ) -> None:
        self.width = width
        self.min_width = min_width
        self.max_height = max_height
        self.anchor = anchor
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.row = row
        self.col = col
        self.margin = margin
        self.visible = visible
        self.non_capturing = non_capturing


class OverlayHandle:
    def hide(self) -> None:
        raise NotImplementedError

    def set_hidden(self, hidden: bool) -> None:
        raise NotImplementedError

    def is_hidden(self) -> bool:
        raise NotImplementedError

    def focus(self) -> None:
        raise NotImplementedError

    def unfocus(self) -> None:
        raise NotImplementedError

    def is_focused(self) -> bool:
        raise NotImplementedError


class InputListenerResult:
    def __init__(self, consume: bool = False, data: str | None = None) -> None:
        self.consume = consume
        self.data = data


InputListener = Callable[[str], InputListenerResult | None]


def _parse_size_value(value: SizeValue | None, reference: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.endswith("%"):
        try:
            pct = float(value[:-1])
            return int(reference * pct / 100)
        except ValueError:
            return None
    return None


class _OverlayEntry:
    def __init__(
        self,
        component: Component,
        options: OverlayOptions | None,
        pre_focus: Component | None,
    ) -> None:
        self.component = component
        self.options = options
        self.pre_focus = pre_focus
        self.hidden = False
        self.focus_order = 0


class Container:
    """A component that contains other components."""

    def __init__(self) -> None:
        self.children: list[Component] = []

    def add_child(self, component: Component) -> None:
        self.children.append(component)

    def remove_child(self, component: Component) -> None:
        try:
            self.children.remove(component)
        except ValueError:
            pass

    def clear(self) -> None:
        self.children.clear()

    def invalidate(self) -> None:
        for child in self.children:
            child.invalidate()

    def render(self, width: int) -> list[str]:
        lines: list[str] = []
        for child in self.children:
            lines.extend(child.render(width))
        return lines


class ScrollbackItem:
    """Wraps a live component in the scrollback for cached re-rendering."""

    __slots__ = ("component", "_lines", "_width")

    def __init__(self, component: Any) -> None:
        self.component = component
        self._lines: list[str] | None = None
        self._width: int = -1

    def get_lines(self, width: int) -> list[str]:
        if self._lines is None or self._width != width:
            self._lines = self.component.render(width)
            self._width = width
        return self._lines

    def invalidate(self) -> None:
        self._lines = None


class TUI(Container):
    """Main class for managing terminal UI with differential rendering."""

    MIN_RENDER_INTERVAL_MS = 16

    def __init__(self, terminal: Terminal | None = None) -> None:
        super().__init__()
        self.terminal = terminal or ProcessTerminal()
        self._previous_lines: list[str] = []
        self._previous_width = 0
        self._previous_height = 0
        self._focused_component: Component | None = None
        self._input_listeners: set[InputListener] = set()
        self._render_requested = False
        self._render_timer: asyncio.TimerHandle | None = None
        self._last_render_at = 0.0
        self._cursor_row = 0
        self._hardware_cursor_row = 0
        self._show_hardware_cursor = os.environ.get("NUU_HARDWARE_CURSOR") == "1"
        self._clear_on_shrink = os.environ.get("NUU_CLEAR_ON_SHRINK") == "1"
        self._max_lines_rendered = 0
        self._previous_viewport_top = 0
        self._full_redraw_count = 0
        self._stopped = False
        self._focus_order_counter = 0
        self._overlay_stack: list[_OverlayEntry] = []
        self._on_debug: Callable[[], None] | None = None
        self._scrollback: list[str | ScrollbackItem] = []
        self._bottom: Component | None = None
        self._input_handler: Callable[[str], None] | None = None
        self._stdin_task: asyncio.Task | None = None
        self._width: int = 80
        self._height: int = 24

    @property
    def full_redraws(self) -> int:
        return self._full_redraw_count

    def get_show_hardware_cursor(self) -> bool:
        return self._show_hardware_cursor

    def set_show_hardware_cursor(self, enabled: bool) -> None:
        if self._show_hardware_cursor == enabled:
            return
        self._show_hardware_cursor = enabled
        if not enabled:
            self.terminal.hide_cursor()
        self.request_render()

    def set_clear_on_shrink(self, enabled: bool) -> None:
        self._clear_on_shrink = enabled

    def set_focus(self, component: Component | None) -> None:
        if isinstance(self._focused_component, Focusable):
            self._focused_component.focused = False
        self._focused_component = component
        if isinstance(component, Focusable):
            component.focused = True

    def show_overlay(self, component: Component, options: OverlayOptions | None = None) -> OverlayHandle:
        entry = _OverlayEntry(component, options, self._focused_component)
        entry.focus_order = self._focus_order_counter = self._focus_order_counter + 1
        self._overlay_stack.append(entry)
        if not options or (not options.non_capturing and self._is_overlay_visible(entry)):
            self.set_focus(component)
        self.terminal.hide_cursor()
        self.request_render()

        class _Handle(OverlayHandle):
            def hide(h) -> None:
                try:
                    idx = self._overlay_stack.index(entry)
                except ValueError:
                    return
                self._overlay_stack.pop(idx)
                if self._focused_component == component:
                    top = self._get_topmost_visible_overlay()
                    self.set_focus(top.component if top else entry.pre_focus)
                if not self._overlay_stack:
                    self.terminal.hide_cursor()
                self.request_render()

            def set_hidden(h, hidden: bool) -> None:
                if entry.hidden == hidden:
                    return
                entry.hidden = hidden
                if hidden:
                    if self._focused_component == component:
                        top = self._get_topmost_visible_overlay()
                        self.set_focus(top.component if top else entry.pre_focus)
                else:
                    if (not options or not options.non_capturing) and self._is_overlay_visible(entry):
                        entry.focus_order = self._focus_order_counter = self._focus_order_counter + 1
                        self.set_focus(component)
                self.request_render()

            def is_hidden(h) -> bool:
                return entry.hidden

            def focus(h) -> None:
                if entry not in self._overlay_stack or not self._is_overlay_visible(entry):
                    return
                if self._focused_component != component:
                    self.set_focus(component)
                entry.focus_order = self._focus_order_counter = self._focus_order_counter + 1
                self.request_render()

            def unfocus(h) -> None:
                if self._focused_component != component:
                    return
                top = self._get_topmost_visible_overlay()
                self.set_focus(top.component if top and top != entry else entry.pre_focus)
                self.request_render()

            def is_focused(h) -> bool:
                return self._focused_component == component

        return _Handle()

    def hide_overlay(self) -> None:
        overlay = self._overlay_stack.pop() if self._overlay_stack else None
        if not overlay:
            return
        if self._focused_component == overlay.component:
            top = self._get_topmost_visible_overlay()
            self.set_focus(top.component if top else overlay.pre_focus)
        if not self._overlay_stack:
            self.terminal.hide_cursor()
        self.request_render()

    def has_overlay(self) -> bool:
        return any(self._is_overlay_visible(o) for o in self._overlay_stack)

    def _is_overlay_visible(self, entry: _OverlayEntry) -> bool:
        if entry.hidden:
            return False
        if entry.options and entry.options.visible:
            return entry.options.visible(self.terminal.columns, self.terminal.rows)
        return True

    def _get_topmost_visible_overlay(self) -> _OverlayEntry | None:
        for i in range(len(self._overlay_stack) - 1, -1, -1):
            entry = self._overlay_stack[i]
            if entry.options and entry.options.non_capturing:
                continue
            if self._is_overlay_visible(entry):
                return entry
        return None

    def invalidate(self) -> None:
        super().invalidate()
        for overlay in self._overlay_stack:
            overlay.component.invalidate()

    def add_input_listener(self, listener: InputListener) -> Callable[[], None]:
        self._input_listeners.add(listener)
        return lambda: self._input_listeners.discard(listener)

    def remove_input_listener(self, listener: InputListener) -> None:
        self._input_listeners.discard(listener)

    def set_bottom(self, component: Component) -> None:
        self._bottom = component

    def emit(self, lines: list[str]) -> None:
        """Add fixed lines to scrollback and request render."""
        self._scrollback.extend(lines)
        self.request_render()

    def emit_component(self, component: Any) -> "ScrollbackItem":
        """Add a live component to scrollback — re-rendered each frame with caching."""
        item = ScrollbackItem(component)
        self._scrollback.append(item)
        self.request_render()
        return item

    def invalidate_scrollback(self) -> None:
        """Mark all live scrollback components dirty so they re-render next frame."""
        for item in self._scrollback:
            if isinstance(item, ScrollbackItem):
                item.invalidate()
        self.request_render()

    def request_render(self, force: bool = False) -> None:
        if force:
            self._previous_lines = []
            self._previous_width = -1
            self._previous_height = -1
            self._cursor_row = 0
            self._hardware_cursor_row = 0
            self._max_lines_rendered = 0
            self._previous_viewport_top = 0
            if self._render_timer:
                self._render_timer.cancel()
                self._render_timer = None
            self._render_requested = True
            loop = asyncio.get_event_loop()
            loop.call_soon(self._do_render_tick)
            return
        if self._render_requested:
            return
        self._render_requested = True
        loop = asyncio.get_event_loop()
        loop.call_soon(self._schedule_render)

    def _schedule_render(self) -> None:
        if self._stopped or self._render_timer or not self._render_requested:
            return
        elapsed = (asyncio.get_event_loop().time() * 1000) - self._last_render_at
        delay = max(0, self.MIN_RENDER_INTERVAL_MS - elapsed) / 1000
        self._render_timer = asyncio.get_event_loop().call_later(delay, self._do_render_tick)

    def _do_render_tick(self) -> None:
        self._render_timer = None
        if self._stopped or not self._render_requested:
            return
        self._render_requested = False
        self._last_render_at = asyncio.get_event_loop().time() * 1000
        self._do_render()
        if self._render_requested:
            self._schedule_render()

    def set_input_handler(self, handler: Callable[[str], None] | None) -> None:
        self._input_handler = handler

    def get_width(self) -> int:
        return self.terminal.columns

    def get_height(self) -> int:
        return self.terminal.rows

    def start(self) -> None:
        self._stopped = False
        self.terminal.start(self._handle_input, self.request_render)
        self.terminal.hide_cursor()
        self.request_render()

    async def run(self) -> None:
        """Run the TUI event loop until stopped."""
        loop = asyncio.get_running_loop()
        stdin_q: asyncio.Queue[bytes] = asyncio.Queue()

        def _read_stdin() -> None:
            try:
                data = os.read(sys.stdin.fileno(), 256)
                if data:
                    stdin_q.put_nowait(data)
            except OSError:
                pass

        loop.add_reader(sys.stdin.fileno(), _read_stdin)

        def _resize() -> None:
            self.request_render(force=True)

        try:
            import signal
            loop.add_signal_handler(signal.SIGWINCH, _resize)
        except (AttributeError, NotImplementedError):
            pass

        try:
            while not self._stopped:
                while not stdin_q.empty():
                    chunk = stdin_q.get_nowait()
                    if isinstance(self.terminal, ProcessTerminal):
                        self.terminal.on_stdin(chunk)
                await asyncio.sleep(0.016)
        finally:
            loop.remove_reader(sys.stdin.fileno())
            try:
                loop.remove_signal_handler(signal.SIGWINCH)
            except Exception:
                pass

    def stop(self) -> None:
        self._stopped = True
        if self._render_timer:
            self._render_timer.cancel()
            self._render_timer = None
        if self._previous_lines:
            target_row = max(0, len(self._previous_lines) - 1)
            diff = target_row - self._hardware_cursor_row
            if diff > 0:
                self.terminal.move_by(diff)
            elif diff < 0:
                self.terminal.move_by(diff)
            self.terminal.write("\r\n")
        self.terminal.show_cursor()
        self.terminal.stop()

    def _handle_input(self, data: str) -> None:
        current = data
        for listener in self._input_listeners:
            result = listener(current)
            if result and result.consume:
                return
            if result and result.data is not None:
                current = result.data
        if not current:
            return

        # Route to app input handler first
        if self._input_handler:
            self._input_handler(current)
            return

        # Global debug key (shift+ctrl+d)
        if self._on_debug:
            if current == "\x1b[100;6u" or current == "\x04":
                self._on_debug()
                return

        # Focused overlay visibility check
        focused_overlay = None
        for o in self._overlay_stack:
            if o.component == self._focused_component:
                focused_overlay = o
                break
        if focused_overlay and not self._is_overlay_visible(focused_overlay):
            top = self._get_topmost_visible_overlay()
            self.set_focus(top.component if top else focused_overlay.pre_focus)

        focused = self._focused_component
        if focused and hasattr(focused, "handle_input"):
            from .keys import is_key_release
            if getattr(focused, "wants_key_release", False):
                pass
            else:
                if is_key_release(current):
                    return
            focused.handle_input(current)
            self.request_render()

    def _resolve_anchor_row(self, anchor: OverlayAnchor, height: int, avail_height: int, margin_top: int) -> int:
        if anchor in ("top-left", "top-center", "top-right"):
            return margin_top
        if anchor in ("bottom-left", "bottom-center", "bottom-right"):
            return margin_top + avail_height - height
        return margin_top + (avail_height - height) // 2

    def _resolve_anchor_col(self, anchor: OverlayAnchor, width: int, avail_width: int, margin_left: int) -> int:
        if anchor in ("top-left", "left-center", "bottom-left"):
            return margin_left
        if anchor in ("top-right", "right-center", "bottom-right"):
            return margin_left + avail_width - width
        return margin_left + (avail_width - width) // 2

    def _resolve_overlay_layout(
        self,
        options: OverlayOptions | None,
        overlay_height: int,
        term_width: int,
        term_height: int,
    ) -> tuple[int, int, int, int | None]:
        opt = options or OverlayOptions()
        margin = (
            OverlayMargin(opt.margin, opt.margin, opt.margin, opt.margin)
            if isinstance(opt.margin, int)
            else (opt.margin or OverlayMargin())
        )
        mt = max(0, margin.top)
        mr = max(0, margin.right)
        mb = max(0, margin.bottom)
        ml = max(0, margin.left)

        avail_w = max(1, term_width - ml - mr)
        avail_h = max(1, term_height - mt - mb)

        width = _parse_size_value(opt.width, term_width) or min(80, avail_w)
        if opt.min_width is not None:
            width = max(width, opt.min_width)
        width = max(1, min(width, avail_w))

        max_height = _parse_size_value(opt.max_height, term_height)
        if max_height is not None:
            max_height = max(1, min(max_height, avail_h))

        effective_height = min(overlay_height, max_height) if max_height is not None else overlay_height

        if opt.row is not None:
            if isinstance(opt.row, str) and opt.row.endswith("%"):
                max_row = max(0, avail_h - effective_height)
                pct = float(opt.row[:-1]) / 100
                row = mt + int(max_row * pct)
            else:
                row = opt.row if isinstance(opt.row, int) else 0
        else:
            row = self._resolve_anchor_row(opt.anchor or "center", effective_height, avail_h, mt)

        if opt.col is not None:
            if isinstance(opt.col, str) and opt.col.endswith("%"):
                max_col = max(0, avail_w - width)
                pct = float(opt.col[:-1]) / 100
                col = ml + int(max_col * pct)
            else:
                col = opt.col if isinstance(opt.col, int) else 0
        else:
            col = self._resolve_anchor_col(opt.anchor or "center", width, avail_w, ml)

        row += opt.offset_y
        col += opt.offset_x
        row = max(mt, min(row, term_height - mb - effective_height))
        col = max(ml, min(col, term_width - mr - width))

        return width, row, col, max_height

    def _composite_overlays(self, lines: list[str], term_width: int, term_height: int) -> list[str]:
        if not self._overlay_stack:
            return lines
        result = list(lines)
        rendered: list[tuple[list[str], int, int, int]] = []
        min_lines_needed = len(result)

        visible = [e for e in self._overlay_stack if self._is_overlay_visible(e)]
        visible.sort(key=lambda e: e.focus_order)
        for entry in visible:
            w, _, _, max_h = self._resolve_overlay_layout(entry.options, 0, term_width, term_height)
            overlay_lines = entry.component.render(w)
            if max_h is not None and len(overlay_lines) > max_h:
                overlay_lines = overlay_lines[:max_h]
            _, row, col, _ = self._resolve_overlay_layout(entry.options, len(overlay_lines), term_width, term_height)
            rendered.append((overlay_lines, row, col, w))
            min_lines_needed = max(min_lines_needed, row + len(overlay_lines))

        working_height = max(len(result), term_height, min_lines_needed)
        while len(result) < working_height:
            result.append("")

        viewport_start = max(0, working_height - term_height)
        for overlay_lines, row, col, w in rendered:
            for i, oline in enumerate(overlay_lines):
                idx = viewport_start + row + i
                if 0 <= idx < len(result):
                    truncated = oline if ansi.visible_width(oline) <= w else ansi.slice_by_column(oline, 0, w, True)
                    result[idx] = self._composite_line_at(result[idx], truncated, col, w, term_width)
        return result

    SEGMENT_RESET = "\x1b[0m\x1b]8;;\x07"

    def _composite_line_at(self, base: str, overlay: str, start_col: int, overlay_width: int, total_width: int) -> str:
        after_start = start_col + overlay_width
        segs = ansi.extract_segments(base, start_col, after_start, total_width - after_start, True)
        before_pad = max(0, start_col - segs.beforeWidth)
        overlay_obj = ansi.slice_with_width(overlay, 0, overlay_width, True)
        overlay_text = overlay_obj[0]
        overlay_w = overlay_obj[1]
        actual_before = max(start_col, segs.beforeWidth)
        actual_overlay = max(overlay_width, overlay_w)
        after_target = max(0, total_width - actual_before - actual_overlay)
        after_pad = max(0, after_target - segs.afterWidth)
        r = self.SEGMENT_RESET
        result = (
            segs.before
            + " " * before_pad
            + r
            + overlay_text
            + " " * max(0, overlay_width - overlay_w)
            + r
            + segs.after
            + " " * after_pad
        )
        vw = ansi.visible_width(result)
        if vw <= total_width:
            return result
        return ansi.slice_by_column(result, 0, total_width, True)

    def _extract_cursor_position(self, lines: list[str], height: int) -> tuple[int, int] | None:
        viewport_top = max(0, len(lines) - height)
        for row in range(len(lines) - 1, viewport_top - 1, -1):
            line = lines[row]
            idx = line.find(CURSOR_MARKER)
            if idx != -1:
                before = line[:idx]
                col = ansi.visible_width(before)
                lines[row] = line[:idx] + line[idx + len(CURSOR_MARKER) :]
                return (row, col)
        return None

    def _position_hardware_cursor(self, cursor_pos: tuple[int, int] | None, total_lines: int) -> None:
        if not cursor_pos or total_lines <= 0:
            self.terminal.hide_cursor()
            return
        target_row = max(0, min(cursor_pos[0], total_lines - 1))
        target_col = max(0, cursor_pos[1])
        row_delta = target_row - self._hardware_cursor_row
        buf = ""
        if row_delta > 0:
            buf += f"\x1b[{row_delta}B"
        elif row_delta < 0:
            buf += f"\x1b[{-row_delta}A"
        buf += f"\x1b[{target_col + 1}G"
        if buf:
            self.terminal.write(buf)
        self._hardware_cursor_row = target_row
        if self._show_hardware_cursor:
            self.terminal.show_cursor()
        else:
            self.terminal.hide_cursor()

    def _do_render(self) -> None:
        if self._stopped:
            return
        width = self.terminal.columns
        height = self.terminal.rows
        width_changed = self._previous_width != 0 and self._previous_width != width
        height_changed = self._previous_height != 0 and self._previous_height != height
        prev_buffer_len = self._previous_height if self._previous_height > 0 else height
        prev_viewport_top = max(0, prev_buffer_len - height)

        # Build base lines: scrollback + bottom component
        # Expand live components from the end backwards, capped at height*4 lines.
        base_lines: list[str] = []
        if self._scrollback:
            max_lines = height * 4
            rev: list[str] = []
            for item in reversed(self._scrollback):
                if isinstance(item, str):
                    rev.append(item)
                else:
                    for line in reversed(item.get_lines(width)):
                        rev.append(line)
                if len(rev) >= max_lines:
                    break
            base_lines = list(reversed(rev[:max_lines]))
        if self._bottom:
            bottom_lines = self._bottom.render(width)
            if base_lines and bottom_lines:
                base_lines.append("")  # separator
            base_lines.extend(bottom_lines)

        new_lines = list(base_lines)

        # Composite overlays
        if self._overlay_stack:
            new_lines = self._composite_overlays(new_lines, width, height)

        # Extract cursor before resets
        cursor_pos = self._extract_cursor_position(new_lines, height)

        # Apply line resets and defensive width truncation.
        # Lines wider than the terminal wrap implicitly, which breaks the
        # differential renderer's assumption of 1 logical line == 1 screen row.
        reset = self.SEGMENT_RESET
        for i in range(len(new_lines)):
            line = ansi.normalize_terminal_output(new_lines[i]) + reset
            if ansi.visible_width(line) > width:
                line = ansi.slice_by_column(line, 0, width, True)
            new_lines[i] = line

        # First render
        if not self._previous_lines and not width_changed and not height_changed:
            self._full_redraw_count += 1
            buf = "\x1b[?2026h"
            for i, line in enumerate(new_lines):
                if i > 0:
                    buf += "\r\n"
                buf += line
            buf += "\x1b[?2026l"
            self.terminal.write(buf)
            self._cursor_row = max(0, len(new_lines) - 1)
            self._hardware_cursor_row = self._cursor_row
            self._max_lines_rendered = len(new_lines)
            self._previous_viewport_top = max(0, len(new_lines) - height)
            self._position_hardware_cursor(cursor_pos, len(new_lines))
            self._previous_lines = new_lines
            self._previous_width = width
            self._previous_height = height
            return

        # Width/height changes or clear-on-shrink need full redraw
        if width_changed or (height_changed and not self._is_termux()) or (self._clear_on_shrink and len(new_lines) < self._max_lines_rendered and not self._overlay_stack):
            self._full_redraw_count += 1
            buf = "\x1b[?2026h\x1b[2J\x1b[H\x1b[3J"
            for i, line in enumerate(new_lines):
                if i > 0:
                    buf += "\r\n"
                buf += line
            buf += "\x1b[?2026l"
            self.terminal.write(buf)
            self._cursor_row = max(0, len(new_lines) - 1)
            self._hardware_cursor_row = self._cursor_row
            self._max_lines_rendered = len(new_lines)
            self._previous_viewport_top = max(0, len(new_lines) - height)
            self._position_hardware_cursor(cursor_pos, len(new_lines))
            self._previous_lines = new_lines
            self._previous_width = width
            self._previous_height = height
            return

        # Find changed lines
        first_changed = -1
        last_changed = -1
        max_lines = max(len(new_lines), len(self._previous_lines))
        for i in range(max_lines):
            old = self._previous_lines[i] if i < len(self._previous_lines) else ""
            new = new_lines[i] if i < len(new_lines) else ""
            if old != new:
                if first_changed == -1:
                    first_changed = i
                last_changed = i

        appended = len(new_lines) > len(self._previous_lines)
        if appended:
            if first_changed == -1:
                first_changed = len(self._previous_lines)
            last_changed = len(new_lines) - 1

        if first_changed == -1:
            self._position_hardware_cursor(cursor_pos, len(new_lines))
            self._previous_height = height
            return

        # All changes are in deleted lines
        if first_changed >= len(new_lines):
            buf = "\x1b[?2026h"
            target_row = max(0, len(new_lines) - 1)
            if target_row < prev_viewport_top:
                # Full redraw needed
                self._full_redraw_count += 1
                buf = "\x1b[?2026h\x1b[2J\x1b[H\x1b[3J"
                for i, line in enumerate(new_lines):
                    if i > 0:
                        buf += "\r\n"
                    buf += line
                buf += "\x1b[?2026l"
                self.terminal.write(buf)
                self._cursor_row = target_row
                self._hardware_cursor_row = target_row
                self._previous_lines = new_lines
                self._previous_width = width
                self._previous_height = height
                self._previous_viewport_top = max(0, len(new_lines) - height)
                self._position_hardware_cursor(cursor_pos, len(new_lines))
                return
            line_diff = target_row - self._hardware_cursor_row
            if line_diff > 0:
                buf += f"\x1b[{line_diff}B"
            elif line_diff < 0:
                buf += f"\x1b[{-line_diff}A"
            buf += "\r"
            extra = len(self._previous_lines) - len(new_lines)
            if extra > height:
                self._full_redraw_count += 1
                buf = "\x1b[?2026h\x1b[2J\x1b[H\x1b[3J"
                for i, line in enumerate(new_lines):
                    if i > 0:
                        buf += "\r\n"
                    buf += line
                buf += "\x1b[?2026l"
                self.terminal.write(buf)
                self._cursor_row = target_row
                self._hardware_cursor_row = target_row
                self._previous_lines = new_lines
                self._previous_width = width
                self._previous_height = height
                self._previous_viewport_top = max(0, len(new_lines) - height)
                self._position_hardware_cursor(cursor_pos, len(new_lines))
                return
            if extra > 0:
                buf += "\x1b[1B"
            for i in range(extra):
                buf += "\r\x1b[2K"
                if i < extra - 1:
                    buf += "\x1b[1B"
            if extra > 0:
                buf += f"\x1b[{extra}A"
            buf += "\x1b[?2026l"
            self.terminal.write(buf)
            self._cursor_row = target_row
            self._hardware_cursor_row = target_row
            self._position_hardware_cursor(cursor_pos, len(new_lines))
            self._previous_lines = new_lines
            self._previous_width = width
            self._previous_height = height
            self._previous_viewport_top = prev_viewport_top
            return

        if first_changed < prev_viewport_top:
            self._full_redraw_count += 1
            buf = "\x1b[?2026h\x1b[2J\x1b[H\x1b[3J"
            for i, line in enumerate(new_lines):
                if i > 0:
                    buf += "\r\n"
                buf += line
            buf += "\x1b[?2026l"
            self.terminal.write(buf)
            self._cursor_row = max(0, len(new_lines) - 1)
            self._hardware_cursor_row = self._cursor_row
            self._previous_lines = new_lines
            self._previous_width = width
            self._previous_height = height
            self._previous_viewport_top = max(0, len(new_lines) - height)
            self._position_hardware_cursor(cursor_pos, len(new_lines))
            return

        # Differential update
        buf = "\x1b[?2026h"
        move_target = first_changed - 1 if appended and first_changed > 0 else first_changed
        line_diff = move_target - self._hardware_cursor_row
        if line_diff > 0:
            buf += f"\x1b[{line_diff}B"
        elif line_diff < 0:
            buf += f"\x1b[{-line_diff}A"
        buf += "\r\n" if appended and first_changed > 0 else "\r"

        render_end = min(last_changed, len(new_lines) - 1)
        for i in range(first_changed, render_end + 1):
            if i > first_changed:
                buf += "\r\n"
            buf += "\x1b[2K"
            line = new_lines[i]
            vw = ansi.visible_width(line)
            if vw > width:
                line = ansi.slice_by_column(line, 0, width, True)
            buf += line

        final_cursor_row = render_end
        if len(self._previous_lines) > len(new_lines):
            if render_end < len(new_lines) - 1:
                move_down = len(new_lines) - 1 - render_end
                buf += f"\x1b[{move_down}B"
                final_cursor_row = len(new_lines) - 1
            extra = len(self._previous_lines) - len(new_lines)
            if extra > 0:
                buf += "\x1b[1B"
            for i in range(extra):
                buf += "\r\x1b[2K"
                if i < extra - 1:
                    buf += "\x1b[1B"
            buf += f"\x1b[{extra}A"

        buf += "\x1b[?2026l"
        self.terminal.write(buf)
        self._cursor_row = max(0, len(new_lines) - 1)
        self._hardware_cursor_row = final_cursor_row
        self._max_lines_rendered = max(self._max_lines_rendered, len(new_lines))
        self._previous_viewport_top = max(prev_viewport_top, final_cursor_row - height + 1)
        self._position_hardware_cursor(cursor_pos, len(new_lines))
        self._previous_lines = new_lines
        self._previous_width = width
        self._previous_height = height

    @staticmethod
    def _is_termux() -> bool:
        return bool(os.environ.get("TERMUX_VERSION"))

    # ------------------------------------------------------------------
    # Context manager (kept for compatibility)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> TUI:
        self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.stop()
