"""Chat message components — Pi TUI style."""

from __future__ import annotations

import os
from ..engine import ansi, theme
from ..engine.component import Box, Container, Text
from ..engine.ansi import italic as _italic
from ..engine.markdown import MarkdownComponent


class UserMessageComponent:
    """User message rendered in a tinted box (Pi: userMsgBg)."""

    def __init__(self, text: str) -> None:
        self._container = Container()
        box = Box(
            padding_x=1, padding_y=1, bg_fn=lambda s: theme.bg("userMessageBg", s)
        )
        box.add(Text(text, padding_x=0, padding_y=0))
        self._container.add(box)

    def render(self, width: int) -> list[str]:
        return self._container.render(width)

    def invalidate(self) -> None:
        self._container.invalidate()


class AssistantMessageComponent:
    """Streaming assistant message — markdown response with optional dimmed thinking."""

    # Class-level flag; set by NuuApp when settings change — no per-render disk read.
    _hide_thinking: bool = False

    @classmethod
    def set_hide_thinking(cls, value: bool) -> None:
        cls._hide_thinking = value

    def __init__(self) -> None:
        self._text = ""
        self._thinking_text = ""
        self._md = MarkdownComponent("", padding_x=1)
        self._container = Container()
        self._container.add(self._md)
        self._finalized = False

    def append(self, delta: str) -> None:
        self._text += delta
        # Don't update markdown component during streaming — Rich parsing on every
        # delta causes lag. Plain text is rendered until finalize().

    def append_thinking(self, delta: str) -> None:
        self._thinking_text += delta

    def set_thinking(self, text: str) -> None:
        self._thinking_text = text

    def finalize(self, full_text: str) -> None:
        self._text = full_text
        self._md.set_text(full_text)
        self._finalized = True

    def render(self, width: int) -> list[str]:
        lines: list[str] = []
        if self._thinking_text:
            if AssistantMessageComponent._hide_thinking:
                lines.append(theme.fg("thinkingText", " ▸ Thinking..."))
            else:
                inner_w = max(1, width - 2)
                for line in Text._wrap(self._thinking_text, inner_w):
                    lines.append(_italic(theme.fg("thinkingText", " " + line)))
            lines.append("")
        if self._finalized:
            lines.extend(self._md.render(width))
        else:
            inner_w = max(1, width - 2)
            for line in Text._wrap(self._text, inner_w):
                lines.append(" " + line)
        return lines

    def invalidate(self) -> None:
        self._container.invalidate()


# ---------------------------------------------------------------------------
# Tool-specific call formatters
# ---------------------------------------------------------------------------

def _shorten_path(path: str | None) -> str:
    """Replace $HOME with ~ for display."""
    if not path:
        return "..."
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path


def _fmt_tool_call(tool_name: str, args: object) -> str:
    """Return a Pi-style one-line call summary for a tool invocation."""
    a = args if isinstance(args, dict) else {}

    def _accent(s: str) -> str:
        return theme.fg("accent", s)

    def _title(s: str) -> str:
        return ansi.bold(theme.fg("accent", s))

    def _dim(s: str) -> str:
        return theme.fg("dim", s)

    name_lower = tool_name.lower()

    if name_lower in ("bash", "execute_command", "run_command"):
        cmd = a.get("command") or a.get("cmd") or ""
        timeout = a.get("timeout")
        suffix = _dim(f" (timeout {timeout}s)") if timeout else ""
        return _title(f"$ {cmd or '...'}") + suffix

    if name_lower in ("read", "read_file", "view", "cat"):
        path = _shorten_path(a.get("file_path") or a.get("path") or a.get("filename"))
        offset = a.get("offset") or a.get("start_line")
        limit = a.get("limit") or a.get("end_line")
        line_range = ""
        if offset and limit:
            line_range = _dim(f":{offset}-{limit}")
        elif offset:
            line_range = _dim(f":{offset}+")
        return _title("read") + " " + _accent(path) + line_range

    if name_lower in ("write", "write_file", "create_file"):
        path = _shorten_path(a.get("file_path") or a.get("path") or a.get("filename"))
        content = a.get("content") or ""
        size = f"{len(content)} chars" if content else ""
        return _title("write") + " " + _accent(path) + (_dim(f"  {size}") if size else "")

    if name_lower in ("edit", "str_replace", "str_replace_editor", "replace_in_file"):
        path = _shorten_path(a.get("file_path") or a.get("path"))
        return _title("edit") + " " + _accent(path)

    if name_lower in ("multiedit", "multi_edit"):
        path = _shorten_path(a.get("file_path") or a.get("path"))
        edits = a.get("edits") or []
        n = len(edits) if isinstance(edits, list) else "?"
        return _title("edit") + " " + _accent(path) + _dim(f"  {n} edits")

    if name_lower in ("grep", "search", "ripgrep", "search_files"):
        pattern = a.get("pattern") or a.get("query") or a.get("regex") or "..."
        path = _shorten_path(a.get("path") or a.get("directory") or a.get("file_path"))
        return _title("grep") + " " + _accent(repr(pattern)) + " " + _dim(path)

    if name_lower in ("find", "find_files", "glob"):
        path = _shorten_path(a.get("path") or a.get("directory") or ".")
        pattern = a.get("pattern") or a.get("glob") or ""
        return _title("find") + " " + _dim(path) + (" " + _accent(pattern) if pattern else "")

    if name_lower in ("ls", "list_directory", "list_files"):
        path = _shorten_path(a.get("path") or a.get("directory") or ".")
        return _title("ls") + " " + _dim(path)

    if name_lower in ("glob", "glob_files"):
        pattern = a.get("pattern") or "..."
        return _title("glob") + " " + _accent(pattern)

    # Generic fallback: tool name + key args compactly
    interesting = {k: v for k, v in a.items() if isinstance(v, (str, int, float, bool)) and k != "content"}
    if interesting:
        pairs = "  ".join(
            f"{k}={repr(v)[:40]}" for k, v in list(interesting.items())[:3]
        )
        return _title(tool_name) + _dim(f"  {pairs}")
    return _title(tool_name)


class ToolExecutionComponent:
    """Tool call: pending -> done/error with Pi-style call header + colored bg."""

    _COLLAPSED_LINES = 5

    def __init__(self, tool_name: str, args: object = None) -> None:
        self._tool_name = tool_name
        self._args = args or {}
        self._done = False
        self._is_error = False
        self._output_lines: list[str] = []
        self._expanded = False
        self._container = Container()
        self._box = Box(padding_x=1, padding_y=0, bg_fn=self._bg)
        self._label = Text(self._call_text(), padding_x=0, padding_y=0)
        self._box.add(self._label)
        self._container.add(self._box)

    def set_done(self, is_error: bool = False, output: str = "") -> None:
        self._done = True
        self._is_error = is_error
        self._output_lines = (
            [line for line in output.splitlines() if line.strip()] if output else []
        )
        self._label.set_text(self._call_text())
        self._box.set_bg_fn(self._bg)

    def toggle_expand(self) -> None:
        self._expanded = not self._expanded

    def _call_text(self) -> str:
        call = _fmt_tool_call(self._tool_name, self._args)
        if not self._done:
            return call
        icon = theme.fg("error", "✗ ") if self._is_error else theme.fg("success", "✓ ")
        return icon + call

    def _bg(self, s: str) -> str:
        if not self._done:
            return theme.bg("toolPendingBg", s)
        if self._is_error:
            return theme.bg("toolErrorBg", s)
        return theme.bg("toolSuccessBg", s)

    def _bg_line(self, text: str, width: int) -> str:
        """Apply bg color to a full-width line."""
        return self._bg(ansi.pad_to_width(text, width))

    def render(self, width: int) -> list[str]:
        base = self._container.render(width)
        if not self._done or not self._output_lines:
            return base
        dim_hex = theme._resolve("dim")
        cap = len(self._output_lines) if self._expanded else self._COLLAPSED_LINES
        for line in self._output_lines[:cap]:
            text = " " + ansi.fg(dim_hex, line[: width - 2])
            base.append(self._bg_line(text, width))
        if not self._expanded and len(self._output_lines) > self._COLLAPSED_LINES:
            remaining = len(self._output_lines) - self._COLLAPSED_LINES
            text = " " + ansi.fg(dim_hex, f"... {remaining} more lines  (Ctrl+O to expand)")
            base.append(self._bg_line(text, width))
        elif self._expanded and len(self._output_lines) > self._COLLAPSED_LINES:
            text = " " + ansi.fg(dim_hex, "(Ctrl+O to collapse)")
            base.append(self._bg_line(text, width))
        # Blank closing line with bg to cap the block
        base.append(self._bg_line("", width))
        return base

    def invalidate(self) -> None:
        self._container.invalidate()


class SystemMessageComponent:
    """System/info message in dim color."""

    def __init__(self, text: str) -> None:
        self._text = Text(text, padding_x=1, padding_y=0)

    def render(self, width: int) -> list[str]:
        lines = []
        for line in self._text._text.splitlines():
            lines.append(theme.fg("dim", " " + line))
        return lines if lines else [theme.fg("dim", "")]

    def invalidate(self) -> None:
        pass


class ErrorMessageComponent:
    """Error message with red left border."""

    def __init__(self, text: str) -> None:
        self._text = text

    def render(self, width: int) -> list[str]:
        result = []
        for line in f"Error: {self._text}".splitlines():
            result.append(theme.fg("error", "│ ") + line)
        return result

    def invalidate(self) -> None:
        pass


class SpinnerComponent:
    """Animated spinner shown while agent is working."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, label: str = "Thinking") -> None:
        self._label = label
        self._frame = 0

    def tick(self) -> None:
        self._frame = (self._frame + 1) % len(self.FRAMES)

    def render(self, width: int) -> list[str]:
        ch = self.FRAMES[self._frame]
        return [theme.fg("dim", f" {ch} {self._label}...")]

    def invalidate(self) -> None:
        pass
