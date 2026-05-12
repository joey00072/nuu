"""
Chat input area. Faithful copy of vibe's ChatInputContainer / ChatInputBody.

Layout:
  SlashPicker          overlay (hidden by default)
  #input-box Vertical  bordered container
    Horizontal
      > Static         prompt label
      _NuuTextArea     auto-growing textarea (1–10 rows)

Keys:
  Enter          submit (or confirm picker selection if open)
  Shift+Enter    insert newline
  Up / Down      navigate picker when open, else cursor movement
  Escape         close picker, or post Escaped when already closed
  Ctrl+C         post Interrupted

Ref: ref/mistral-vibe/vibe/cli/textual_ui/widgets/chat_input/
"""

from __future__ import annotations

from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static, TextArea

from ...coding_agent.slash_commands import BUILTIN_SLASH_COMMANDS
from .slash_picker import SlashPicker

_SLASH_ENTRIES: list[tuple[str, str]] = [
    (cmd.name, cmd.description) for cmd in BUILTIN_SLASH_COMMANDS
]


class _NuuTextArea(TextArea):
    """TextArea that routes Enter/Up/Down through the slash picker first."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("shift+enter,ctrl+j", "insert_newline", show=False, priority=True),
    ]

    class SubmitRequested(Message):
        pass

    def __init__(self, picker: SlashPicker, text: str = "", **kwargs: object) -> None:
        super().__init__(text, **kwargs)
        self._picker = picker

    def action_insert_newline(self) -> None:
        self.insert("\n")

    async def _on_key(self, event: events.Key) -> None:
        # Picker intercepts nav keys when open — must come first
        if self._picker.is_open():
            match event.key:
                case "up":
                    event.prevent_default()
                    event.stop()
                    await self._picker.move_up()
                    return
                case "down":
                    event.prevent_default()
                    event.stop()
                    await self._picker.move_down()
                    return
                case "enter" | "tab":
                    event.prevent_default()
                    event.stop()
                    await self._picker.confirm()
                    return
                case "escape":
                    event.prevent_default()
                    event.stop()
                    await self._picker.hide()
                    return

        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.SubmitRequested())
            return

        await super()._on_key(event)

    def on_blur(self, _event: events.Blur) -> None:
        if not self.disabled:
            self.call_after_refresh(self.focus)


class ChatInput(Widget):
    """Bordered input area with overlay slash-command picker."""

    DEFAULT_CSS = """
    ChatInput {
        height: auto;
        width: 100%;
    }
    ChatInput #input-box {
        height: auto;
        width: 100%;
        border: solid ansi_bright_black;
        border-title-align: right;
        border-title-color: ansi_bright_black;
        padding: 0 1;
    }
    ChatInput #input-box.-disabled {
        opacity: 0.5;
    }
    ChatInput #input-box Horizontal {
        height: auto;
        width: 100%;
    }
    ChatInput #prompt {
        width: auto;
        height: auto;
        color: $accent;
        text-style: bold;
        padding: 0 1 0 0;
    }
    ChatInput _NuuTextArea {
        width: 1fr;
        height: auto;
        min-height: 1;
        max-height: 10;
        background: transparent;
        color: ansi_default;
        border: none;
        padding: 0;
        scrollbar-visibility: hidden;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+c", "interrupt", show=False),
        Binding("escape", "escape_pressed", show=False, priority=True),
    ]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class SlashCommand(Message):
        def __init__(self, command: str) -> None:
            self.command = command
            super().__init__()

    class Interrupted(Message):
        pass

    class Escaped(Message):
        pass

    def __init__(self, model_label: str = "", **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._model_label = model_label
        self._picker: SlashPicker | None = None
        self._textarea: _NuuTextArea | None = None
        self._disabled = False

    def compose(self) -> ComposeResult:
        self._picker = SlashPicker(_SLASH_ENTRIES)
        yield self._picker

        with Vertical(id="input-box") as box:
            box.border_title = self._model_label
            with Horizontal():
                yield Static(">", id="prompt")
                self._textarea = _NuuTextArea(
                    self._picker,
                    "",
                    soft_wrap=True,
                    show_line_numbers=False,
                    compact=True,
                    tab_behavior="focus",
                    highlight_cursor_line=False,
                    id="textarea",
                )
                yield self._textarea

    def on_mount(self) -> None:
        self.focus_input()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on__nuu_text_area_submit_requested(
        self, _event: _NuuTextArea.SubmitRequested
    ) -> None:
        if self._disabled:
            return
        text = self._get_text().strip()
        if not text:
            return
        if text.startswith("/"):
            cmd = text[1:].strip().split()[0] if text[1:].strip() else ""
            self._clear()
            self.post_message(self.SlashCommand(cmd))
        else:
            self._clear()
            self.post_message(self.Submitted(text))

    async def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._picker is None:
            return
        text = event.text_area.text
        if text.startswith("/"):
            filtered = self._picker.filter_commands(text[1:])
            if filtered:
                await self._picker.show_suggestions(filtered, 0)
                self._position_picker(len(filtered))
            else:
                await self._picker.hide()
        else:
            await self._picker.hide()

    def on_slash_picker_selected(self, event: SlashPicker.Selected) -> None:
        self._clear()
        self.post_message(self.SlashCommand(event.command))

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def action_interrupt(self) -> None:
        self.post_message(self.Interrupted())

    async def action_escape_pressed(self) -> None:
        if self._picker and self._picker.is_open():
            await self._picker.hide()
        else:
            self.post_message(self.Escaped())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def focus_input(self) -> None:
        if self._textarea:
            self._textarea.focus()

    def set_enabled(self, enabled: bool) -> None:
        self._disabled = not enabled
        box = self.query_one("#input-box")
        if enabled:
            box.remove_class("-disabled")
        else:
            box.add_class("-disabled")
        if self._textarea:
            self._textarea.disabled = not enabled

    def set_model_label(self, label: str) -> None:
        self._model_label = label
        try:
            self.query_one("#input-box").border_title = label
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _get_text(self) -> str:
        return self._textarea.text if self._textarea else ""

    def _clear(self) -> None:
        if self._textarea:
            self._textarea.clear()

    def _position_picker(self, suggestion_count: int) -> None:
        """Position the picker just above the cursor (vibe's _position_popup formula)."""
        if not self._textarea or not self._picker:
            return
        try:
            cursor = self._textarea.cursor_screen_offset
            my_region = self.region
            popup_height = min(suggestion_count, 10) + 2  # +2 for border
            x = cursor.x - my_region.x
            y = cursor.y - popup_height - my_region.y
            self._picker.styles.offset = (x, y)
        except Exception:
            pass
