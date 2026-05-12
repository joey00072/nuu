"""
Tests for nuu.tui Pi-style components.
No real LLM calls, no Textual.
"""

from __future__ import annotations


from nuu.tui.engine.ansi import visible_width, strip_ansi
from nuu.tui.engine.component import Container, Text, Box, Spacer
from nuu.tui.engine.editor import Editor
from nuu.tui.widgets.messages import (
    AssistantMessageComponent,
    ToolExecutionComponent,
    UserMessageComponent,
    SystemMessageComponent,
)
from nuu.tui.widgets.footer import FooterComponent
from nuu.tui.widgets.slash_picker import SlashPickerComponent
from nuu.tui.widgets.scoped_models_selector import (
    _is_enabled,
    _toggle,
    _enable_all,
    _clear_all,
    _move,
    _sorted_ids,
)


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------


def test_visible_width_plain():
    assert visible_width("hello") == 5


def test_visible_width_strips_ansi():
    colored = "\x1b[38;2;255;0;0mhello\x1b[0m"
    assert visible_width(colored) == 5


def test_strip_ansi():
    assert strip_ansi("\x1b[1mbold\x1b[0m") == "bold"


# ---------------------------------------------------------------------------
# Component primitives
# ---------------------------------------------------------------------------


def test_text_renders_content():
    t = Text("hello world")
    lines = t.render(40)
    assert any("hello world" in strip_ansi(l) for l in lines)


def test_text_wraps_long_line():
    t = Text("a b c d e f g h", padding_x=0)
    lines = t.render(5)
    assert len(lines) >= 2


def test_spacer():
    s = Spacer(3)
    lines = s.render(40)
    assert len(lines) == 3


def test_box_pads_lines():
    b = Box(padding_x=1, padding_y=0)
    b.add(Text("hi", padding_x=0))
    lines = b.render(20)
    assert len(lines) >= 1
    # Box adds left padding
    assert strip_ansi(lines[0]).startswith(" ")


def test_container_stacks_children():
    c = Container()
    c.add(Text("line1"))
    c.add(Text("line2"))
    combined = strip_ansi(" ".join(c.render(40)))
    assert "line1" in combined
    assert "line2" in combined


# ---------------------------------------------------------------------------
# Message components
# ---------------------------------------------------------------------------


def test_user_message_renders():
    msg = UserMessageComponent("hello agent")
    lines = msg.render(60)
    combined = " ".join(strip_ansi(l) for l in lines)
    assert "hello agent" in combined


def test_assistant_message_append_finalize():
    msg = AssistantMessageComponent()
    msg.append("Hello")
    msg.append(", world")
    assert "Hello, world" in msg._text
    msg.finalize("Hello, world!")
    assert msg._text == "Hello, world!"


def test_assistant_message_renders():
    msg = AssistantMessageComponent()
    msg.finalize("some response")
    lines = msg.render(60)
    combined = " ".join(strip_ansi(l) for l in lines)
    assert "some response" in combined


def test_assistant_message_thinking_renders_dim():
    msg = AssistantMessageComponent()
    msg.append_thinking("Let me think about this...")
    msg.finalize("Final answer")
    lines = msg.render(60)
    combined = " ".join(strip_ansi(l) for l in lines)
    assert "Let me think about this" in combined
    assert "Final answer" in combined


def test_tool_execution_pending():
    tw = ToolExecutionComponent("bash")
    lines = tw.render(60)
    combined = " ".join(strip_ansi(l) for l in lines)
    assert "$" in combined


def test_tool_execution_done():
    tw = ToolExecutionComponent("grep")
    tw.set_done(is_error=False)
    lines = tw.render(60)
    combined = " ".join(strip_ansi(l) for l in lines)
    assert "grep" in combined
    assert "✓" in combined


def test_tool_execution_error():
    tw = ToolExecutionComponent("edit")
    tw.set_done(is_error=True)
    lines = tw.render(60)
    combined = " ".join(strip_ansi(l) for l in lines)
    assert "edit" in combined
    assert "✗" in combined


def test_system_message():
    sm = SystemMessageComponent("Session cleared.")
    lines = sm.render(60)
    combined = " ".join(strip_ansi(l) for l in lines)
    assert "Session cleared." in combined


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------


def test_footer_renders_two_lines():
    f = FooterComponent("openai/gpt-4o", "/home/user/project")
    lines = f.render(80)
    assert len(lines) == 2


def test_footer_shows_model():
    f = FooterComponent("anthropic/claude-3", "/tmp")
    lines = f.render(80)
    combined = " ".join(strip_ansi(l) for l in lines)
    assert "anthropic/claude-3" in combined


def test_footer_busy_state():
    f = FooterComponent("openai/gpt-4o", "/tmp")
    f.set_busy(False)
    lines_idle = " ".join(strip_ansi(l) for l in f.render(80))
    f.set_busy(True)
    lines_busy = " ".join(strip_ansi(l) for l in f.render(80))
    assert "idle" in lines_idle
    assert "busy" in lines_busy


# ---------------------------------------------------------------------------
# Editor
# ---------------------------------------------------------------------------


def test_editor_insert_text():
    e = Editor()
    e.handle_input("h")
    e.handle_input("i")
    assert e.text == "hi"


def test_editor_backspace():
    e = Editor()
    for ch in "hello":
        e.handle_input(ch)
    e.handle_input("\x7f")
    assert e.text == "hell"


def test_editor_clear():
    e = Editor()
    e.handle_input("test")
    e.clear()
    assert e.text == ""


def test_editor_submit_fires_callback():
    results = []
    e = Editor()
    e.on_submit = results.append
    for ch in "hello":
        e.handle_input(ch)
    e.handle_input("\r")
    assert results == ["hello"]


def test_editor_renders_with_cursor():
    e = Editor()
    e.handle_input("test")
    lines = e.render(40)
    combined = " ".join(strip_ansi(l) for l in lines)
    assert "test" in combined


def test_editor_ctrl_u_clears_line():
    e = Editor()
    for ch in "hello":
        e.handle_input(ch)
    e.handle_input("\x15")
    assert e.text == ""


# ---------------------------------------------------------------------------
# Slash picker
# ---------------------------------------------------------------------------


def test_slash_picker_filter():
    picker = SlashPickerComponent([("model", "Select model"), ("new", "New session"), ("quit", "Quit")])
    matches = picker.filter_commands("mo")
    assert len(matches) == 1
    assert matches[0][0] == "model"


def test_slash_picker_show_hide():
    picker = SlashPickerComponent([("quit", "Quit"), ("new", "New")])
    assert not picker.is_open()
    picker.show([("quit", "Quit")])
    assert picker.is_open()
    picker.hide()
    assert not picker.is_open()


def test_slash_picker_confirm():
    picker = SlashPickerComponent([("model", "desc")])
    picker.show([("model", "Select model")])
    cmd = picker.confirm()
    assert cmd == "model"
    assert not picker.is_open()


def test_slash_picker_renders_when_open():
    picker = SlashPickerComponent([("quit", "Quit"), ("new", "New")])
    picker.show([("quit", "Quit"), ("new", "New")])
    lines = picker.render(60)
    combined = " ".join(strip_ansi(l) for l in lines)
    assert "quit" in combined
    assert "new" in combined


def test_slash_picker_empty_when_closed():
    picker = SlashPickerComponent([("quit", "Quit")])
    assert picker.render(60) == []


ALL = ["a/1", "a/2", "b/1", "b/2"]


def test_is_enabled_null():
    assert _is_enabled(None, "a/1") is True


def test_is_enabled_explicit():
    assert _is_enabled(["a/1"], "a/1") is True
    assert _is_enabled(["a/1"], "b/1") is False


def test_toggle_from_null():
    """First toggle when all enabled: start with ONLY that model."""
    assert _toggle(None, ALL, "a/1") == ["a/1"]


def test_toggle_add():
    assert _toggle(["a/1"], ALL, "b/1") == ["a/1", "b/1"]


def test_toggle_remove():
    assert _toggle(["a/1", "b/1"], ALL, "a/1") == ["b/1"]


def test_toggle_remove_last():
    """Removing the last item yields an empty list, not None."""
    assert _toggle(["a/1"], ALL, "a/1") == []


def test_enable_all_from_null():
    assert _enable_all(None, ALL) is None


def test_enable_all_partial():
    assert _enable_all(["a/1"], ALL, ["a/2"]) == ["a/1", "a/2"]


def test_enable_all_targets():
    assert _enable_all(["a/1"], ALL, ["a/2", "b/1"]) == ["a/1", "a/2", "b/1"]


def test_enable_all_becomes_null():
    """Enabling everything collapses back to None (all enabled)."""
    assert _enable_all(["a/1"], ALL, ALL) is None


def test_clear_all_from_null():
    assert _clear_all(None, ALL) == []


def test_clear_all_targets_from_null():
    assert _clear_all(None, ALL, ["a/1", "a/2"]) == ["b/1", "b/2"]


def test_clear_all_explicit():
    assert _clear_all(["a/1", "a/2"], ALL) == []


def test_clear_all_partial():
    assert _clear_all(["a/1", "a/2", "b/1"], ALL, ["a/1"]) == ["a/2", "b/1"]


def test_move_basic():
    assert _move(["a/1", "a/2", "b/1"], "a/2", -1) == ["a/2", "a/1", "b/1"]
    assert _move(["a/1", "a/2", "b/1"], "a/1", 1) == ["a/2", "a/1", "b/1"]


def test_move_bounds():
    """Moving past the edge is a no-op."""
    assert _move(["a/1", "a/2"], "a/1", -1) == ["a/1", "a/2"]
    assert _move(["a/1", "a/2"], "a/2", 1) == ["a/1", "a/2"]


def test_move_from_null():
    assert _move(None, ALL, -1) is None


def test_sorted_ids_null():
    assert _sorted_ids(None, ALL) == ALL


def test_sorted_ids_explicit():
    assert _sorted_ids(["b/1", "a/1"], ALL) == ["b/1", "a/1", "a/2", "b/2"]
