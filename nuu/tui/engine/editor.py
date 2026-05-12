"""Multi-line text editor component for the nuu TUI.

Ported from Pi's ref/pi/packages/tui/src/components/editor.ts.
Features: undo stack, kill ring/yank, jump-to-char, history navigation.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable

from . import ansi, theme
from .keybindings import get_keybindings


@dataclass
class EditorState:
    lines: list[str] = field(default_factory=lambda: [""])
    cursor_line: int = 0
    cursor_col: int = 0


def _copy_state(s: EditorState) -> EditorState:
    return EditorState(lines=list(s.lines), cursor_line=s.cursor_line, cursor_col=s.cursor_col)


class TextChunk:
    def __init__(self, text: str, start_index: int, end_index: int):
        self.text = text
        self.start_index = start_index
        self.end_index = end_index

class LayoutLine:
    def __init__(self, text: str, has_cursor: bool, cursor_pos: int | None = None):
        self.text = text
        self.has_cursor = has_cursor
        self.cursor_pos = cursor_pos

def _is_whitespace_char(c: str) -> bool:
    return c.isspace()

def _word_wrap_line(line: str, max_width: int) -> list[TextChunk]:
    if not line or max_width <= 0:
        return [TextChunk("", 0, 0)]
        
    line_width = ansi.visible_width(line)
    if line_width <= max_width:
        return [TextChunk(line, 0, len(line))]
        
    import grapheme
    chunks = []
    segments = list(grapheme.graphemes(line))
    
    current_width = 0
    chunk_start_char_idx = 0
    
    wrap_opp_index = -1
    wrap_opp_width = 0
    wrap_opp_char_idx = -1
    
    char_idx = 0
    
    for i, seg in enumerate(segments):
        g_width = ansi.grapheme_width(seg)
        is_ws = _is_whitespace_char(seg)
        
        if current_width + g_width > max_width:
            if wrap_opp_index >= 0 and current_width - wrap_opp_width + g_width <= max_width:
                chunks.append(TextChunk(line[chunk_start_char_idx:wrap_opp_char_idx], chunk_start_char_idx, wrap_opp_char_idx))
                chunk_start_char_idx = wrap_opp_char_idx
                current_width -= wrap_opp_width
            elif chunk_start_char_idx < char_idx:
                chunks.append(TextChunk(line[chunk_start_char_idx:char_idx], chunk_start_char_idx, char_idx))
                chunk_start_char_idx = char_idx
                current_width = 0
            wrap_opp_index = -1
            wrap_opp_char_idx = -1
            
        if g_width > max_width:
            chunks.append(TextChunk(line[chunk_start_char_idx:char_idx], chunk_start_char_idx, char_idx))
            chunk_start_char_idx = char_idx
            current_width = 0
            continue
            
        current_width += g_width
        
        next_seg = segments[i + 1] if i + 1 < len(segments) else None
        if is_ws and next_seg and not _is_whitespace_char(next_seg):
            wrap_opp_index = i + 1
            wrap_opp_width = current_width
            wrap_opp_char_idx = char_idx + len(seg)
            
        char_idx += len(seg)
        
    chunks.append(TextChunk(line[chunk_start_char_idx:], chunk_start_char_idx, len(line)))
    return chunks


class Editor:
    """Multi-line editor with undo, kill ring, yank, and jump-to-char."""

    on_submit: Callable[[str], None] | None = None
    on_interrupt: Callable[[], None] | None = None
    on_escape: Callable[[], None] | None = None
    on_change: Callable[[str], None] | None = None
    on_ctrl_d: Callable[[], None] | None = None

    def __init__(self, model_label: str = "") -> None:
        self._state = EditorState()
        self._model_label = model_label
        self._disabled = False
        self._history: list[str] = []
        self._history_index = -1
        self._last_width = 80
        self._scroll_offset = 0

        # Undo stack — list of state snapshots (newest at end)
        self._undo_stack: list[EditorState] = []

        # Kill ring — list of killed strings (newest at end)
        self._kill_ring: list[str] = []
        self._last_action: str | None = None  # "kill" | "yank" | "type-word" | None
        self._last_yank_text: str | None = None  # for yank-pop

        # Jump-to-char mode
        self._jump_mode: str | None = None  # "forward" | "backward" | None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def text(self) -> str:
        return "\n".join(self._state.lines)

    @property
    def cursor_line(self) -> int:
        return self._state.cursor_line

    @property
    def cursor_col(self) -> int:
        return self._state.cursor_col

    def extract_at_prefix(self) -> str | None:
        """
        Look at the text before the cursor on the current line.
        If we find an '@' that starts a token (preceded by whitespace or
        at the start of the line), return the text from '@' to cursor.
        Returns None if no valid @-prefix is found.
        """
        line = self._state.lines[self._state.cursor_line]
        col = self._state.cursor_col
        text_before = line[:col]

        # Walk backwards from cursor to find the start of the current token
        i = col - 1
        while i >= 0 and text_before[i] not in (' ', '\t', '"', "'"):
            i -= 1

        token_start = i + 1
        token = text_before[token_start:col]

        if token.startswith("@") and len(token) > 1:
            return token  # e.g. "@src/foo"
        if token == "@":
            return "@"
        return None

    def insert_text_at_cursor(self, text: str) -> None:
        """Insert text at current cursor position, replacing any @-prefix."""
        self._history_index = -1
        self._push_undo()
        self._last_action = None

        # Remove the @-prefix first
        line = self._state.lines[self._state.cursor_line]
        col = self._state.cursor_col
        text_before = line[:col]

        i = col - 1
        while i >= 0 and line[i] not in (' ', '\t', '"', "'"):
            i -= 1
        token_start = i + 1

        # Replace the token with the inserted text
        before_token = line[:token_start]
        after_cursor = line[col:]
        self._state.lines[self._state.cursor_line] = before_token + text + after_cursor
        self._state.cursor_col = token_start + len(text)

        if self.on_change:
            self.on_change(self.text)

    def set_text(self, text: str) -> None:
        self._history_index = -1
        lines = text.split("\n") if text else [""]
        self._state.lines = lines
        self._state.cursor_line = len(lines) - 1
        self._state.cursor_col = len(lines[-1])
        self._undo_stack.clear()
        self._last_action = None

    def clear(self) -> None:
        self._history_index = -1
        self._state = EditorState()
        self._undo_stack.clear()
        self._last_action = None
        self._jump_mode = None

    def set_model_label(self, label: str) -> None:
        self._model_label = label

    def set_disabled(self, disabled: bool) -> None:
        self._disabled = disabled

    def invalidate(self) -> None:
        pass

    def add_to_history(self, text: str) -> None:
        trimmed = text.strip()
        if not trimmed:
            return
        if self._history and self._history[0] == trimmed:
            return
        self._history.insert(0, trimmed)
        if len(self._history) > 100:
            self._history.pop()

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    def handle_input(self, data: str) -> None:
        if self._disabled:
            return

        kb = get_keybindings()

        # Ctrl+C
        if kb.matches(data, "tui.input.copy"):
            if self.on_interrupt:
                self.on_interrupt()
            return

        # Undo (Ctrl+-)
        if kb.matches(data, "tui.editor.undo"):
            self._undo()
            return

        # Jump-to-char mode: waiting for the target character
        if self._jump_mode is not None:
            # Pressing the jump key again cancels
            if kb.matches(data, "tui.editor.jumpForward") or kb.matches(data, "tui.editor.jumpBackward"):
                self._jump_mode = None
                return
            from .keys import decode_printable_key
            printable = decode_printable_key(data)
            if printable is None and data and 32 <= ord(data[0]):
                printable = data[0]
            if printable:
                direction = self._jump_mode
                self._jump_mode = None
                self._jump_to_char(printable, direction)
                return
            # Control char — cancel jump mode and fall through
            self._jump_mode = None

        # Yank (Ctrl+Y)
        if kb.matches(data, "tui.editor.yank"):
            self._yank()
            return

        # Yank-pop (Alt+Y)
        if kb.matches(data, "tui.editor.yankPop"):
            self._yank_pop()
            return

        # Deletion
        if kb.matches(data, "tui.editor.deleteToLineEnd"):
            self._delete_to_line_end()
            return
        if kb.matches(data, "tui.editor.deleteToLineStart"):
            self._delete_to_line_start()
            return
        if kb.matches(data, "tui.editor.deleteWordBackward"):
            self._delete_word_backward()
            return
        if kb.matches(data, "tui.editor.deleteWordForward"):
            self._delete_word_forward()
            return
        if kb.matches(data, "tui.editor.deleteCharBackward"):
            self._backspace()
            return
        if kb.matches(data, "tui.editor.deleteCharForward"):
            if not self.text and self.on_ctrl_d:
                self.on_ctrl_d()
            else:
                self._delete_forward()
            return

        # Cursor movement
        if kb.matches(data, "tui.editor.cursorLineStart"):
            self._state.cursor_col = 0
            self._last_action = None
            return
        if kb.matches(data, "tui.editor.cursorLineEnd"):
            self._state.cursor_col = len(self._state.lines[self._state.cursor_line])
            self._last_action = None
            return
        if kb.matches(data, "tui.editor.cursorWordLeft"):
            self._move_word_backward()
            self._last_action = None
            return
        if kb.matches(data, "tui.editor.cursorWordRight"):
            self._move_word_forward()
            self._last_action = None
            return

        # Jump-to-char mode triggers
        if kb.matches(data, "tui.editor.jumpForward"):
            self._jump_mode = "forward"
            return
        if kb.matches(data, "tui.editor.jumpBackward"):
            self._jump_mode = "backward"
            return

        # Tab — do not insert; handled at app level for slash picker / thinking cycle
        if kb.matches(data, "tui.input.tab"):
            return

        # New line
        if kb.matches(data, "tui.input.newLine"):
            self._add_new_line()
            return

        # Submit
        if kb.matches(data, "tui.input.submit"):
            if self.on_submit:
                result = self.text.strip()
                if result:
                    self.add_to_history(result)
                    self._state = EditorState()
                    self._history_index = -1
                    self._scroll_offset = 0
                    self._undo_stack.clear()
                    self._last_action = None
                    self._jump_mode = None
                    self.on_submit(result)
                    if self.on_change:
                        self.on_change("")
            return

        # Arrow keys with history
        if kb.matches(data, "tui.editor.cursorUp"):
            self._last_action = None
            if self._is_editor_empty() and self._history:
                self._navigate_history(-1)
            elif self._history_index > -1 and self._state.cursor_line == 0:
                self._navigate_history(-1)
            elif self._state.cursor_line > 0:
                self._state.cursor_line -= 1
                self._state.cursor_col = min(self._state.cursor_col, len(self._state.lines[self._state.cursor_line]))
            else:
                self._state.cursor_col = 0
            return

        if kb.matches(data, "tui.editor.cursorDown"):
            self._last_action = None
            if self._history_index > -1 and self._state.cursor_line >= len(self._state.lines) - 1:
                self._navigate_history(1)
            elif self._state.cursor_line < len(self._state.lines) - 1:
                self._state.cursor_line += 1
                self._state.cursor_col = min(self._state.cursor_col, len(self._state.lines[self._state.cursor_line]))
            else:
                self._state.cursor_col = len(self._state.lines[self._state.cursor_line])
            return

        if kb.matches(data, "tui.editor.pageUp"):
            self._last_action = None
            page = max(1, 8)
            self._state.cursor_line = max(0, self._state.cursor_line - page)
            self._state.cursor_col = min(self._state.cursor_col, len(self._state.lines[self._state.cursor_line]))
            return

        if kb.matches(data, "tui.editor.pageDown"):
            self._last_action = None
            page = max(1, 8)
            self._state.cursor_line = min(len(self._state.lines) - 1, self._state.cursor_line + page)
            self._state.cursor_col = min(self._state.cursor_col, len(self._state.lines[self._state.cursor_line]))
            return

        if kb.matches(data, "tui.editor.cursorLeft"):
            self._last_action = None
            if self._state.cursor_col > 0:
                self._state.cursor_col -= 1
            elif self._state.cursor_line > 0:
                self._state.cursor_line -= 1
                self._state.cursor_col = len(self._state.lines[self._state.cursor_line])
            return

        if kb.matches(data, "tui.editor.cursorRight"):
            self._last_action = None
            line = self._state.lines[self._state.cursor_line]
            if self._state.cursor_col < len(line):
                self._state.cursor_col += 1
            elif self._state.cursor_line < len(self._state.lines) - 1:
                self._state.cursor_line += 1
                self._state.cursor_col = 0
            return

        # Escape
        if kb.matches(data, "tui.select.cancel"):
            self._last_action = None
            if self.on_escape:
                self.on_escape()
            return

        # Printable character
        printable = _decode_printable(data)
        if printable is not None and len(printable) > 0:
            self._insert(printable)
            return

        # Fallback: raw printable bytes (tab excluded — handled above)
        if data and not data.startswith("\x1b") and all(32 <= ord(c) for c in data):
            self._insert(data)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def _is_editor_empty(self) -> bool:
        return len(self._state.lines) == 1 and self._state.lines[0] == ""

    def _navigate_history(self, direction: int) -> None:
        if not self._history:
            return
        new_index = self._history_index - direction
        if new_index < -1 or new_index >= len(self._history):
            return
        self._history_index = new_index
        if self._history_index == -1:
            self._state = EditorState()
        else:
            text = self._history[self._history_index]
            lines = text.split("\n")
            self._state.lines = lines or [""]
            self._state.cursor_line = len(self._state.lines) - 1
            self._state.cursor_col = len(self._state.lines[-1])

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def _push_undo(self) -> None:
        self._undo_stack.append(_copy_state(self._state))
        if len(self._undo_stack) > 200:
            self._undo_stack.pop(0)

    def _undo(self) -> None:
        self._history_index = -1
        snapshot = self._undo_stack.pop() if self._undo_stack else None
        if snapshot is None:
            return
        self._state = snapshot
        self._last_action = None
        if self.on_change:
            self.on_change(self.text)

    # ------------------------------------------------------------------
    # Kill ring
    # ------------------------------------------------------------------

    def _kill_push(self, text: str, prepend: bool) -> None:
        if not text:
            return
        if self._last_action == "kill" and self._kill_ring:
            last = self._kill_ring[-1]
            self._kill_ring[-1] = (text + last) if prepend else (last + text)
        else:
            self._kill_ring.append(text)
        if len(self._kill_ring) > 50:
            self._kill_ring.pop(0)

    def _yank(self) -> None:
        if not self._kill_ring:
            return
        self._push_undo()
        text = self._kill_ring[-1]
        self._insert_text(text)
        self._last_yank_text = text
        self._last_action = "yank"

    def _yank_pop(self) -> None:
        if self._last_action != "yank" or len(self._kill_ring) <= 1:
            return
        self._push_undo()
        # Delete the previously yanked text
        if self._last_yank_text:
            self._delete_yanked_text(self._last_yank_text)
        # Rotate ring: move newest to oldest
        last = self._kill_ring.pop()
        self._kill_ring.insert(0, last)
        # Insert new newest
        text = self._kill_ring[-1]
        self._insert_text(text)
        self._last_yank_text = text
        self._last_action = "yank"

    def _delete_yanked_text(self, text: str) -> None:
        lines = text.split("\n")
        if len(lines) == 1:
            line = self._state.lines[self._state.cursor_line]
            n = len(text)
            col = self._state.cursor_col
            self._state.lines[self._state.cursor_line] = line[: col - n] + line[col:]
            self._state.cursor_col = max(0, col - n)
        else:
            start_line = self._state.cursor_line - (len(lines) - 1)
            start_col = len(self._state.lines[start_line]) - len(lines[0])
            after = self._state.lines[self._state.cursor_line][self._state.cursor_col :]
            before = self._state.lines[start_line][:start_col]
            self._state.lines[start_line : self._state.cursor_line + 1] = [before + after]
            self._state.cursor_line = start_line
            self._state.cursor_col = start_col

    # ------------------------------------------------------------------
    # Jump-to-char
    # ------------------------------------------------------------------

    def _jump_to_char(self, char: str, direction: str) -> None:
        self._last_action = None
        forward = direction == "forward"
        lines = self._state.lines
        step = 1 if forward else -1
        end = len(lines) if forward else -1

        line_idx = self._state.cursor_line
        while line_idx != end:
            line = lines[line_idx]
            is_current = line_idx == self._state.cursor_line
            if forward:
                search_from = (self._state.cursor_col + 1) if is_current else 0
                idx = line.find(char, search_from)
            else:
                search_to = (self._state.cursor_col - 1) if is_current else len(line)
                idx = line.rfind(char, 0, search_to)
            if idx != -1:
                self._state.cursor_line = line_idx
                self._state.cursor_col = idx
                return
            line_idx += step

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def render(self, width: int) -> list[str]:
        title = f" {self._model_label} " if self._model_label else ""
        border_hex = theme._resolve("borderMuted")

        top_border = self._render_top_border(width, title)
        content_lines = self._render_content(width)
        bottom_border = ansi.fg(border_hex, "─" * width)

        result = [top_border]
        result.extend(content_lines)
        result.append(bottom_border)
        return result

    def _render_top_border(self, width: int, title: str) -> str:
        border_hex = theme._resolve("borderMuted")
        if not title:
            return ansi.fg(border_hex, "─" * width)
        title_colored = ansi.fg(border_hex, title)
        title_w = ansi.visible_width(title)
        remaining = max(0, width - title_w)
        left = remaining // 2
        right = remaining - left
        return (
            ansi.fg(border_hex, "─" * left)
            + title_colored
            + ansi.fg(border_hex, "─" * right)
        )

    def _layout_text(self, content_width: int) -> list[LayoutLine]:
        layout_lines: list[LayoutLine] = []
        if not self._state.lines or (len(self._state.lines) == 1 and self._state.lines[0] == ""):
            layout_lines.append(LayoutLine("", True, self._state.cursor_col))
            return layout_lines

        for i, line in enumerate(self._state.lines):
            is_current_line = (i == self._state.cursor_line)
            line_vis_width = ansi.visible_width(line)

            if line_vis_width <= content_width:
                if is_current_line:
                    layout_lines.append(LayoutLine(line, True, self._state.cursor_col))
                else:
                    layout_lines.append(LayoutLine(line, False))
            else:
                chunks = _word_wrap_line(line, content_width)
                for chunk_index, chunk in enumerate(chunks):
                    if not chunk:
                        continue
                    
                    cursor_pos = self._state.cursor_col
                    is_last_chunk = (chunk_index == len(chunks) - 1)
                    
                    has_cursor_in_chunk = False
                    adjusted_cursor_pos = 0

                    if is_current_line:
                        if is_last_chunk:
                            has_cursor_in_chunk = cursor_pos >= chunk.start_index
                            adjusted_cursor_pos = cursor_pos - chunk.start_index
                        else:
                            has_cursor_in_chunk = chunk.start_index <= cursor_pos < chunk.end_index
                            if has_cursor_in_chunk:
                                adjusted_cursor_pos = cursor_pos - chunk.start_index
                                if adjusted_cursor_pos > len(chunk.text):
                                    adjusted_cursor_pos = len(chunk.text)

                    if has_cursor_in_chunk:
                        layout_lines.append(LayoutLine(chunk.text, True, adjusted_cursor_pos))
                    else:
                        layout_lines.append(LayoutLine(chunk.text, False))
        return layout_lines

    def _render_content(self, width: int) -> list[str]:
        prompt = theme.fg("accent", "> ")
        prompt_w = 2
        inner_w = max(1, width - prompt_w)
        self._last_width = inner_w
        
        layout_lines = self._layout_text(inner_w)
        
        max_visible = max(5, 8)
        
        cursor_line_index = next((i for i, line in enumerate(layout_lines) if line.has_cursor), 0)
        
        if cursor_line_index < self._scroll_offset:
            self._scroll_offset = cursor_line_index
        elif cursor_line_index >= self._scroll_offset + max_visible:
            self._scroll_offset = cursor_line_index - max_visible + 1
            
        max_scroll_offset = max(0, len(layout_lines) - max_visible)
        self._scroll_offset = max(0, min(self._scroll_offset, max_scroll_offset))
        
        visible_lines = layout_lines[self._scroll_offset : self._scroll_offset + max_visible]
        
        result: list[str] = []
        for i, layout_line in enumerate(visible_lines):
            display_text = layout_line.text
            line_vis_width = ansi.visible_width(display_text)
            
            if layout_line.has_cursor and layout_line.cursor_pos is not None:
                col = min(layout_line.cursor_pos, len(display_text))
                before = display_text[:col]
                at = display_text[col] if col < len(display_text) else " "
                after = display_text[col + 1:] if col < len(display_text) else ""
                cursor_char = f"{ansi.REVERSE}{at}{ansi.REVERSE_OFF}"
                rendered = before + cursor_char + after
                rendered = ansi.pad_to_width(rendered, inner_w)
            else:
                rendered = ansi.pad_to_width(display_text, inner_w)
            
            prefix = prompt if (self._scroll_offset + i) == 0 else " " * prompt_w
            result.append(prefix + rendered)
            
        return result

    # ------------------------------------------------------------------
    # Internal editing
    # ------------------------------------------------------------------

    def _insert(self, text: str) -> None:
        self._history_index = -1
        for ch in text:
            # Coalesce consecutive word chars into one undo unit (like Pi)
            is_space = ch in (" ", "\t", "\n")
            if is_space or self._last_action != "type-word":
                self._push_undo()
            self._last_action = None if is_space else "type-word"

            if ch == "\n":
                line = self._state.lines[self._state.cursor_line]
                before = line[: self._state.cursor_col]
                after = line[self._state.cursor_col:]
                self._state.lines[self._state.cursor_line] = before
                self._state.cursor_line += 1
                self._state.lines.insert(self._state.cursor_line, after)
                self._state.cursor_col = 0
            else:
                line = self._state.lines[self._state.cursor_line]
                self._state.lines[self._state.cursor_line] = (
                    line[: self._state.cursor_col] + ch + line[self._state.cursor_col:]
                )
                self._state.cursor_col += 1
        if self.on_change:
            self.on_change(self.text)

    def _insert_text(self, text: str) -> None:
        """Insert multi-char text without per-char undo logic (for yank)."""
        self._history_index = -1
        lines = text.split("\n")
        if len(lines) == 1:
            line = self._state.lines[self._state.cursor_line]
            before = line[: self._state.cursor_col]
            after = line[self._state.cursor_col:]
            self._state.lines[self._state.cursor_line] = before + text + after
            self._state.cursor_col += len(text)
        else:
            line = self._state.lines[self._state.cursor_line]
            before = line[: self._state.cursor_col]
            after = line[self._state.cursor_col:]
            self._state.lines[self._state.cursor_line] = before + lines[0]
            for i in range(1, len(lines) - 1):
                self._state.cursor_line += 1
                self._state.lines.insert(self._state.cursor_line, lines[i])
            self._state.cursor_line += 1
            self._state.lines.insert(self._state.cursor_line, lines[-1] + after)
            self._state.cursor_col = len(lines[-1])
        if self.on_change:
            self.on_change(self.text)

    def _backspace(self) -> None:
        self._history_index = -1
        self._push_undo()
        self._last_action = None
        if self._state.cursor_col > 0:
            line = self._state.lines[self._state.cursor_line]
            self._state.lines[self._state.cursor_line] = (
                line[: self._state.cursor_col - 1] + line[self._state.cursor_col:]
            )
            self._state.cursor_col -= 1
        elif self._state.cursor_line > 0:
            prev = self._state.lines[self._state.cursor_line - 1]
            curr = self._state.lines.pop(self._state.cursor_line)
            self._state.cursor_line -= 1
            self._state.cursor_col = len(prev)
            self._state.lines[self._state.cursor_line] = prev + curr
        if self.on_change:
            self.on_change(self.text)

    def _delete_forward(self) -> None:
        self._history_index = -1
        self._push_undo()
        self._last_action = None
        line = self._state.lines[self._state.cursor_line]
        if self._state.cursor_col < len(line):
            self._state.lines[self._state.cursor_line] = (
                line[: self._state.cursor_col] + line[self._state.cursor_col + 1:]
            )
        elif self._state.cursor_line < len(self._state.lines) - 1:
            next_line = self._state.lines.pop(self._state.cursor_line + 1)
            self._state.lines[self._state.cursor_line] += next_line
        if self.on_change:
            self.on_change(self.text)

    def _delete_word_backward(self) -> None:
        self._history_index = -1
        line = self._state.lines[self._state.cursor_line]
        if self._state.cursor_col == 0:
            if self._state.cursor_line > 0:
                self._push_undo()
                was_kill = self._last_action == "kill"
                self._kill_push("\n", prepend=True)
                self._last_action = "kill"
                prev = self._state.lines[self._state.cursor_line - 1]
                curr = self._state.lines.pop(self._state.cursor_line)
                self._state.cursor_line -= 1
                self._state.cursor_col = len(prev)
                self._state.lines[self._state.cursor_line] = prev + curr
        else:
            self._push_undo()
            was_kill = self._last_action == "kill"
            old_col = self._state.cursor_col
            self._move_word_backward()
            deleted = line[self._state.cursor_col : old_col]
            self._kill_push(deleted, prepend=True)
            self._last_action = "kill"
            self._state.lines[self._state.cursor_line] = (
                line[: self._state.cursor_col] + line[old_col:]
            )
        if self.on_change:
            self.on_change(self.text)

    def _delete_word_forward(self) -> None:
        self._history_index = -1
        line = self._state.lines[self._state.cursor_line]
        if self._state.cursor_col >= len(line):
            if self._state.cursor_line < len(self._state.lines) - 1:
                self._push_undo()
                self._kill_push("\n", prepend=False)
                self._last_action = "kill"
                next_line = self._state.lines.pop(self._state.cursor_line + 1)
                self._state.lines[self._state.cursor_line] += next_line
        else:
            self._push_undo()
            was_kill = self._last_action == "kill"
            old_col = self._state.cursor_col
            self._move_word_forward()
            deleted = line[old_col : self._state.cursor_col]
            self._state.cursor_col = old_col
            self._kill_push(deleted, prepend=False)
            self._last_action = "kill"
            self._state.lines[self._state.cursor_line] = (
                line[:old_col] + line[old_col + len(deleted):]
            )
        if self.on_change:
            self.on_change(self.text)

    def _delete_to_line_start(self) -> None:
        self._history_index = -1
        line = self._state.lines[self._state.cursor_line]
        if self._state.cursor_col > 0:
            self._push_undo()
            deleted = line[: self._state.cursor_col]
            self._kill_push(deleted, prepend=True)
            self._last_action = "kill"
            self._state.lines[self._state.cursor_line] = line[self._state.cursor_col:]
            self._state.cursor_col = 0
        elif self._state.cursor_line > 0:
            self._push_undo()
            self._kill_push("\n", prepend=True)
            self._last_action = "kill"
            prev = self._state.lines[self._state.cursor_line - 1]
            curr = self._state.lines.pop(self._state.cursor_line)
            self._state.cursor_line -= 1
            self._state.cursor_col = len(prev)
            self._state.lines[self._state.cursor_line] = prev + curr
        if self.on_change:
            self.on_change(self.text)

    def _delete_to_line_end(self) -> None:
        self._history_index = -1
        line = self._state.lines[self._state.cursor_line]
        if self._state.cursor_col < len(line):
            self._push_undo()
            deleted = line[self._state.cursor_col:]
            self._kill_push(deleted, prepend=False)
            self._last_action = "kill"
            self._state.lines[self._state.cursor_line] = line[: self._state.cursor_col]
        elif self._state.cursor_line < len(self._state.lines) - 1:
            self._push_undo()
            self._kill_push("\n", prepend=False)
            self._last_action = "kill"
            next_line = self._state.lines.pop(self._state.cursor_line + 1)
            self._state.lines[self._state.cursor_line] += next_line
        if self.on_change:
            self.on_change(self.text)

    def _add_new_line(self) -> None:
        self._history_index = -1
        self._push_undo()
        self._last_action = None
        line = self._state.lines[self._state.cursor_line]
        before = line[: self._state.cursor_col]
        after = line[self._state.cursor_col:]
        self._state.lines[self._state.cursor_line] = before
        self._state.cursor_line += 1
        self._state.lines.insert(self._state.cursor_line, after)
        self._state.cursor_col = 0
        if self.on_change:
            self.on_change(self.text)

    def _move_word_backward(self) -> None:
        line = self._state.lines[self._state.cursor_line]
        pos = self._state.cursor_col
        while pos > 0 and line[pos - 1] == " ":
            pos -= 1
        while pos > 0 and line[pos - 1] != " ":
            pos -= 1
        self._state.cursor_col = pos

    def _move_word_forward(self) -> None:
        line = self._state.lines[self._state.cursor_line]
        pos = self._state.cursor_col
        while pos < len(line) and line[pos] == " ":
            pos += 1
        while pos < len(line) and line[pos] != " ":
            pos += 1
        self._state.cursor_col = pos


# ---------------------------------------------------------------------------
# Printable decoding helper
# ---------------------------------------------------------------------------


def _decode_printable(data: str) -> str | None:
    from .keys import decode_printable_key
    return decode_printable_key(data)
