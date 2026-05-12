"""Tests for nuu.tui.engine.TUI — differential rendering and width safety."""

from __future__ import annotations

from nuu.tui.engine.tui import TUI


class _FakeTerm:
    """Minimal terminal mock for TUI tests."""

    def __init__(self, columns: int = 80, rows: int = 24) -> None:
        self.columns = columns
        self.rows = rows
        self._writes: list[str] = []

    def write(self, data: str) -> None:
        self._writes.append(data)

    def show_cursor(self) -> None:
        pass

    def hide_cursor(self) -> None:
        pass

    def get_size(self) -> tuple[int, int]:
        return (self.columns, self.rows)

    def start(self, on_input=None, on_resize=None) -> None:
        pass

    def stop(self) -> None:
        pass


class _WideComponent:
    """Component that renders a line wider than the terminal."""

    def __init__(self, text: str) -> None:
        self._text = text

    def render(self, width: int) -> list[str]:
        return [self._text]

    def invalidate(self) -> None:
        pass


def test_truncates_wide_lines_on_first_render():
    """Lines wider than terminal must be truncated so the terminal does not
    implicitly wrap them and desynchronize the differential renderer."""
    term = _FakeTerm(columns=20, rows=10)
    tui = TUI(terminal=term)
    tui.start()

    wide = "x" * 100
    tui.emit([wide])
    tui._do_render()

    # The written buffer should not contain the full 100-character line raw.
    # After truncation it should be exactly 20 display columns.
    buf = "".join(term._writes)
    # Find the rendered line content (after clear-screen sequence)
    assert "x" * 20 in buf
    assert "x" * 21 not in buf


def test_truncates_wide_live_component():
    """Live scrollback components that produce wide lines must also be clipped."""
    term = _FakeTerm(columns=15, rows=10)
    tui = TUI(terminal=term)
    tui.start()

    comp = _WideComponent("overflow text here")
    tui.emit_component(comp)
    tui._do_render()

    buf = "".join(term._writes)
    # The text is 18 chars; truncated to 15.
    assert "overflow text h" in buf
    assert "overflow text here" not in buf


def test_differential_update_with_changing_width():
    """Changing line width between renders must not leave trailing debris."""
    term = _FakeTerm(columns=30, rows=10)
    tui = TUI(terminal=term)
    tui.start()

    # First render — line exactly fits
    tui.emit(["fit"])
    tui._do_render()
    first_buf = "".join(term._writes)
    assert "fit" in first_buf

    # Second render — line is now too wide and must be truncated
    tui._scrollback.clear()
    tui.emit(["this line is way too long for the terminal"])
    tui._do_render()
    second_buf = "".join(term._writes)
    # Should contain truncated version, not full string
    assert "this line is way too long for" in second_buf
    assert "terminal" not in second_buf
