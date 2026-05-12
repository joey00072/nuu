"""Status bar widget docked at the bottom of the TUI."""

from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    """One-line footer showing model name and agent state."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $surface;
        color: ansi_bright_black;
        padding: 0 1;
        dock: bottom;
    }
    StatusBar.busy {
        color: $accent;
    }
    """

    def __init__(self, model_name: str, **kwargs: object) -> None:
        super().__init__(self._fmt(model_name, busy=False), **kwargs)
        self._model = model_name

    @staticmethod
    def _fmt(model: str, *, busy: bool) -> str:
        state = "⠋ busy" if busy else "● idle"
        return f" {model}   {state}"

    def set_busy(self, busy: bool) -> None:
        self.update(self._fmt(self._model, busy=busy))
        if busy:
            self.add_class("busy")
        else:
            self.remove_class("busy")

    def set_model(self, label: str) -> None:
        self._model = label
        self.update(self._fmt(label, busy=self.has_class("busy")))
