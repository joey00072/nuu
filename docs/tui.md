# nuu/tui

Custom raw-terminal rendering engine. No Textual. All output is ANSI escape
sequences with differential updates. Ported from Pi's `ref/pi/packages/tui/src/`.

## Responsibility

Owns the terminal: raw mode, input parsing, screen layout, component rendering,
overlays, and scrollback. `coding_agent` subscribes to agent events and calls
into the TUI; the TUI knows nothing about LLM providers or coding tools.

## Layout

```
engine/          Core engine
  tui.py         TUI class — differential render, overlays, scrollback
  terminal.py    ProcessTerminal — raw mode, Kitty protocol, stdin buffering
  editor.py      Editor — multi-line input, history, cursor movement
  keys.py        Key parsing — parse_kitty_sequence, decode_kitty_printable, matches_key
  keybindings.py KeybindingsManager, TUI_KEYBINDINGS, get_keybindings()
  component.py   Component base, Container, Text, Box, Spacer
  markdown.py    MarkdownComponent renderer
  ansi.py        ANSI helpers — visible_width, strip_ansi, fg, pad_to_width
  theme.py       Color theme constants

widgets/         App-specific components
  messages.py    AssistantMessageComponent, UserMessageComponent,
                 ToolExecutionComponent, SystemMessageComponent,
                 ErrorMessageComponent, SpinnerComponent
  footer.py      FooterComponent
  slash_picker.py, model_picker.py, login_picker.py
  api_key_input.py, chat_input.py, status_bar.py

app.py           NuuApp + _BottomComponent — application wiring
```

`widgets/spinner.py` is stale (Textual reference). Active spinner is
`SpinnerComponent` in `widgets/messages.py`.

## Input Flow

```
ProcessTerminal (raw stdin bytes)
  -> StdinBuffer (sequence reassembly)
  -> TUI._handle_input(data: str)
  -> NuuApp._handle_input(data: str)
       is_key_release(data) -> return early              ← must be first
       kb.matches("tui.input.copy") -> _on_interrupt()   ← Ctrl+C always works
       kb.matches("tui.select.cancel") and busy -> _on_escape()
       active overlay (api_key_input/auth_type/login_picker/model_picker)
         -> overlay.handle_input(data)
       slash_picker open -> nav keys handled, others fall through
       Editor.handle_input(data)
```

## Kitty Keyboard Protocol

On startup, `ProcessTerminal` queries Kitty support (`\x1b[?u`). If confirmed,
enables extended mode (`\x1b[>7u`). This sends three events per key: press
(`:1u`), repeat (`:2u`), release (`:3u`).

Release events must be dropped at two points:
1. `app._handle_input` — `is_key_release(data)` guard at the top.
2. `decode_kitty_printable()` in `keys.py` — returns `None` for event type 3.

Missing either guard causes double character insertion.

## Rendering

- `tui.set_bottom(component)` — pins a component to the bottom of the viewport.
- `tui.emit(lines)` — appends to the scrollback buffer.
- `tui.request_render()` — schedules a differential re-render.
- Render only writes lines that changed since the last pass.

Overlays (model picker, login picker, API key input, auth type selector) are
managed by `_BottomComponent` in `app.py` via `set_model_picker()`,
`set_login_picker()`, `set_api_key_input()`, `set_auth_type()`, `clear_overlays()`.
The TUI engine has overlay primitives but the app does not use them for pickers.

## Bottom Area Render Order

`_BottomComponent.render()` order (must be preserved):

1. Tool execution display (if active)
2. Streaming assistant content (if active)
3. Spinner — always **below** content, never above

## Key Behaviors

**Escape**: close active overlay → abort agent if busy → do nothing when idle.
Escape never quits.

**Ctrl+C**: abort if busy. When idle: first press clears editor; second press
within 500 ms quits.

**Ctrl+D**: quit when editor is empty (fires `editor.on_ctrl_d` → `_on_ctrl_d`).
When editor is not empty, behaves as delete-char-forward.

## Keybindings

All bindings live in `engine/keybindings.py` → `TUI_KEYBINDINGS`. Match via
`get_keybindings().matches(data, "tui.binding.name")`. Never check raw escape
sequences in application code.

## Components

Everything renderable implements `Component.render(width) -> list[str]`.
Interactive components extend `Focusable` and implement `handle_input(data) -> bool`.
Compose layout with `Container`, `Box`, `Text`, `Spacer`. No Textual imports anywhere.
