"""
Braille spinner widget for use during agent processing.

Shows an animated braille character that cycles on a 0.1s timer.
Call start() / stop(success) to control it.

Ref: ref/mistral-vibe/vibe/cli/textual_ui/widgets/spinner.py
"""

from __future__ import annotations

from textual.widgets import Static

_BRAILLE_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class Spinner(Static):
    """Animated braille spinner that updates on a repeating timer."""

    DEFAULT_CSS = """
    Spinner {
        width: auto;
        height: 1;
    }
    """

    def __init__(self, label: str = "Thinking", **kwargs: object) -> None:
        super().__init__(_BRAILLE_FRAMES[0], **kwargs)
        self._label = label
        self._pos = 0
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._tick)

    def on_unmount(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._pos = (self._pos + 1) % len(_BRAILLE_FRAMES)
        self.update(f"{_BRAILLE_FRAMES[self._pos]} {self._label}…")
