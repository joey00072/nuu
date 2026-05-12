"""Terminal interface and process-terminal implementation.

Ported from Pi's ref/pi/packages/tui/src/terminal.ts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import termios
import tty
from typing import Callable

from .keys import set_kitty_protocol_active

log = logging.getLogger("nuu.terminal")

TERMINAL_PROGRESS_KEEPALIVE_MS = 1000
TERMINAL_PROGRESS_ACTIVE = "\x1b]9;4;3\x07"
TERMINAL_PROGRESS_CLEAR = "\x1b]9;4;0;\x07"


class Terminal:
    """Minimal terminal interface for TUI."""

    def start(
        self,
        on_input: Callable[[str], None],
        on_resize: Callable[[], None],
    ) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    async def drain_input(self, max_ms: float = 1000, idle_ms: float = 50) -> None:
        raise NotImplementedError

    def write(self, data: str) -> None:
        raise NotImplementedError

    @property
    def columns(self) -> int:
        raise NotImplementedError

    @property
    def rows(self) -> int:
        raise NotImplementedError

    @property
    def kitty_protocol_active(self) -> bool:
        raise NotImplementedError

    def move_by(self, lines: int) -> None:
        raise NotImplementedError

    def hide_cursor(self) -> None:
        raise NotImplementedError

    def show_cursor(self) -> None:
        raise NotImplementedError

    def clear_line(self) -> None:
        raise NotImplementedError

    def clear_from_cursor(self) -> None:
        raise NotImplementedError

    def clear_screen(self) -> None:
        raise NotImplementedError

    def set_title(self, title: str) -> None:
        raise NotImplementedError

    def set_progress(self, active: bool) -> None:
        raise NotImplementedError


class StdinBuffer:
    """Splits batched stdin into individual sequences."""

    def __init__(self, timeout: float = 0.01) -> None:
        self._timeout = timeout
        self._buffer = ""
        self._on_data: Callable[[str], None] | None = None
        self._on_paste: Callable[[str], None] | None = None
        self._timer: asyncio.TimerHandle | None = None

    def set_handlers(
        self,
        on_data: Callable[[str], None] | None = None,
        on_paste: Callable[[str], None] | None = None,
    ) -> None:
        self._on_data = on_data
        self._on_paste = on_paste

    def process(self, data: str) -> None:
        self._buffer += data
        if self._timer:
            self._timer.cancel()
        loop = asyncio.get_event_loop()
        self._timer = loop.call_later(self._timeout, self._flush)

    def _flush(self) -> None:
        self._timer = None
        buf = self._buffer
        self._buffer = ""
        if not buf:
            return

        # Handle bracketed paste
        while True:
            start = buf.find("\x1b[200~")
            if start == -1:
                break
            end = buf.find("\x1b[201~", start)
            if end == -1:
                # Incomplete paste - put back in buffer
                self._buffer = buf[start:]
                buf = buf[:start]
                break
            prefix = buf[:start]
            paste_content = buf[start + 7 : end]
            suffix = buf[end + 6 :]
            self._emit_data(prefix)
            if self._on_paste:
                self._on_paste(paste_content)
            buf = suffix

        self._emit_data(buf)

    def _emit_data(self, text: str) -> None:
        if not text or not self._on_data:
            return
        # Split into individual escape sequences and printable chars
        i = 0
        while i < len(text):
            if text[i] == "\x1b":
                j = i + 1
                if j < len(text) and text[j] == "[":
                    j += 1
                    while j < len(text) and text[j] not in "ABCDEFGHJKLMSTfZ~mGKSsu":
                        j += 1
                    if j < len(text):
                        j += 1
                elif j < len(text) and text[j] == "O":
                    j += 2
                elif j < len(text) and text[j] == "]":
                    while j < len(text) and text[j] not in "\x07":
                        if text[j] == "\x1b" and j + 1 < len(text) and text[j + 1] == "\\":
                            j += 2
                            break
                        j += 1
                    if j < len(text) and text[j] == "\x07":
                        j += 1
                elif j < len(text):
                    j += 1
                self._on_data(text[i:j])
                i = j
            else:
                # Single printable/control char
                self._on_data(text[i])
                i += 1

    def destroy(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None


class ProcessTerminal(Terminal):
    """Real terminal using process stdin/stdout."""

    def __init__(self) -> None:
        self._was_raw = False
        self._input_handler: Callable[[str], None] | None = None
        self._resize_handler: Callable[[], None] | None = None
        self._kitty_protocol_active = False
        self._modify_other_keys_active = False
        self._stdin_buffer: StdinBuffer | None = None
        self._progress_interval: asyncio.TimerHandle | None = None
        self._orig_termios: list | None = None

    @property
    def kitty_protocol_active(self) -> bool:
        return self._kitty_protocol_active

    def start(
        self,
        on_input: Callable[[str], None],
        on_resize: Callable[[], None],
    ) -> None:
        self._input_handler = on_input
        self._resize_handler = on_resize

        fd = sys.stdin.fileno()
        try:
            self._orig_termios = termios.tcgetattr(fd)
            tty.setraw(fd)
            self._was_raw = True
        except (termios.error, OSError):
            self._was_raw = False

        sys.stdin.reconfigure(encoding="utf-8", errors="replace")

        # Enable bracketed paste
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()

        # Set up stdin buffer
        self._stdin_buffer = StdinBuffer(timeout=0.01)

        def _on_data(sequence: str) -> None:
            # Kitty protocol response pattern: \x1b[?<flags>u
            if not self._kitty_protocol_active:
                if re.match(r"^\x1b\[\?\d+u$", sequence):
                    self._kitty_protocol_active = True
                    set_kitty_protocol_active(True)
                    # Enable Kitty keyboard protocol
                    sys.stdout.write("\x1b[>7u")
                    sys.stdout.flush()
                    return
            if self._input_handler:
                self._input_handler(sequence)

        def _on_paste(content: str) -> None:
            if self._input_handler:
                self._input_handler(f"\x1b[200~{content}\x1b[201~")

        self._stdin_buffer.set_handlers(_on_data, _on_paste)

        # Query Kitty protocol
        sys.stdout.write("\x1b[?u")
        sys.stdout.flush()

        # Fallback to modifyOtherKeys after 150ms
        loop = asyncio.get_event_loop()
        loop.call_later(0.15, self._enable_modify_other_keys)

    def _enable_modify_other_keys(self) -> None:
        if not self._kitty_protocol_active and not self._modify_other_keys_active:
            sys.stdout.write("\x1b[>4;2m")
            sys.stdout.flush()
            self._modify_other_keys_active = True

    def on_stdin(self, data: bytes) -> None:
        try:
            text = data.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        if self._stdin_buffer:
            self._stdin_buffer.process(text)

    async def drain_input(self, max_ms: float = 1000, idle_ms: float = 50) -> None:
        if self._kitty_protocol_active:
            sys.stdout.write("\x1b[<u")
            sys.stdout.flush()
            self._kitty_protocol_active = False
            set_kitty_protocol_active(False)
        if self._modify_other_keys_active:
            sys.stdout.write("\x1b[>4;0m")
            sys.stdout.flush()
            self._modify_other_keys_active = False

        previous_handler = self._input_handler
        self._input_handler = None

        last_data_time = asyncio.get_event_loop().time()
        end_time = last_data_time + max_ms / 1000

        def _on_data(_: bytes) -> None:
            nonlocal last_data_time
            last_data_time = asyncio.get_event_loop().time()

        # Temporarily hook stdin
        try:
            while True:
                now = asyncio.get_event_loop().time()
                if now >= end_time:
                    break
                if now - last_data_time >= idle_ms / 1000:
                    break
                await asyncio.sleep(min(idle_ms / 1000, end_time - now))
        finally:
            self._input_handler = previous_handler

    def stop(self) -> None:
        if self._progress_interval:
            self._progress_interval.cancel()
            sys.stdout.write(TERMINAL_PROGRESS_CLEAR)
            sys.stdout.flush()

        # Disable bracketed paste
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()

        if self._kitty_protocol_active:
            sys.stdout.write("\x1b[<u")
            sys.stdout.flush()
            self._kitty_protocol_active = False
            set_kitty_protocol_active(False)
        if self._modify_other_keys_active:
            sys.stdout.write("\x1b[>4;0m")
            sys.stdout.flush()
            self._modify_other_keys_active = False

        if self._stdin_buffer:
            self._stdin_buffer.destroy()
            self._stdin_buffer = None

        if self._orig_termios is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._orig_termios)
            except (termios.error, OSError):
                pass
            self._orig_termios = None

        self._input_handler = None

    def write(self, data: str) -> None:
        sys.stdout.write(data)
        sys.stdout.flush()

    @property
    def columns(self) -> int:
        try:
            return os.get_terminal_size().columns
        except OSError:
            return int(os.environ.get("COLUMNS", 80))

    @property
    def rows(self) -> int:
        try:
            return os.get_terminal_size().lines
        except OSError:
            return int(os.environ.get("LINES", 24))

    def move_by(self, lines: int) -> None:
        if lines > 0:
            self.write(f"\x1b[{lines}B")
        elif lines < 0:
            self.write(f"\x1b[{-lines}A")

    def hide_cursor(self) -> None:
        self.write("\x1b[?25l")

    def show_cursor(self) -> None:
        self.write("\x1b[?25h")

    def clear_line(self) -> None:
        self.write("\x1b[K")

    def clear_from_cursor(self) -> None:
        self.write("\x1b[J")

    def clear_screen(self) -> None:
        self.write("\x1b[2J\x1b[H")

    def set_title(self, title: str) -> None:
        self.write(f"\x1b]0;{title}\x07")

    def set_progress(self, active: bool) -> None:
        if active:
            self.write(TERMINAL_PROGRESS_ACTIVE)
            if self._progress_interval:
                self._progress_interval.cancel()
            loop = asyncio.get_event_loop()
            self._progress_interval = loop.call_later(
                TERMINAL_PROGRESS_KEEPALIVE_MS / 1000, self._progress_keepalive
            )
        else:
            if self._progress_interval:
                self._progress_interval.cancel()
                self._progress_interval = None
            self.write(TERMINAL_PROGRESS_CLEAR)

    def _progress_keepalive(self) -> None:
        self.write(TERMINAL_PROGRESS_ACTIVE)
        loop = asyncio.get_event_loop()
        self._progress_interval = loop.call_later(
            TERMINAL_PROGRESS_KEEPALIVE_MS / 1000, self._progress_keepalive
        )
