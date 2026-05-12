"""
NuuApp: Pi-style TUI for the nuu coding agent.

Layout (top → bottom):
  [chat messages — scrolls upward naturally]
  ─────────────────────────────────────────────────
  [spinner / active tool / streaming text  (if busy)]
  [slash picker  (if open)]
  [model picker  (if open)  OR  editor]
  [footer]

No Textual. Raw terminal + bottom-pinned ANSI renderer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import sys
from typing import Any

from ..agent.types import (
    AgentEndEvent,
    AgentEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from ..coding_agent.session import AgentSession
from ..coding_agent.slash_commands import BUILTIN_SLASH_COMMANDS
from .engine import ansi, theme
from .engine.editor import Editor
from .engine.tui import TUI, ScrollbackItem
from .widgets.footer import FooterComponent
from .widgets.messages import (
    AssistantMessageComponent,
    ErrorMessageComponent,
    SpinnerComponent,
    SystemMessageComponent,
    ToolExecutionComponent,
    UserMessageComponent,
)
from .widgets.model_picker import ModelPickerComponent
from .widgets.login_picker import (
    LoginPickerComponent,
    AuthTypeComponent,
    OAUTH_PROVIDER_IDS,
)
from .widgets.api_key_input import ApiKeyInputComponent
from .widgets.slash_picker import SlashPickerComponent
from .widgets.settings_picker import SettingsPickerComponent
from .widgets.scoped_models_selector import ScopedModelsSelectorComponent
from .widgets.at_picker import AtPickerComponent, AtMatch
from .widgets.tree_selector import TreeSelectorComponent, TreeNode

log = logging.getLogger("nuu.app")

_SLASH_ENTRIES: list[tuple[str, str]] = [
    (cmd.name, cmd.description) for cmd in BUILTIN_SLASH_COMMANDS
]


class _BottomComponent:
    """
    Bottom-pinned area rendered in place.

    Contains (top → bottom):
      spinner · active tool · streaming text  (while busy)
      blank separator
      slash picker  (if open)
      overlay (login/api-key input/model picker)  OR  editor
      blank separator
      footer
    """

    def __init__(
        self,
        spinner: SpinnerComponent,
        slash_picker: SlashPickerComponent,
        at_picker: AtPickerComponent,
        editor: Editor,
        footer: FooterComponent,
    ) -> None:
        self._spinner = spinner
        self._slash_picker = slash_picker
        self._at_picker = at_picker
        self._editor = editor
        self._footer = footer
        self._model_picker: ModelPickerComponent | None = None
        self._settings_picker: SettingsPickerComponent | None = None
        self._scoped_models: ScopedModelsSelectorComponent | None = None
        self._login_picker: LoginPickerComponent | None = None
        self._auth_type: AuthTypeComponent | None = None
        self._api_key_input: ApiKeyInputComponent | None = None
        self._tree_selector: TreeSelectorComponent | None = None
        self._show_spinner = False
        self._streaming: AssistantMessageComponent | None = None
        self._current_tool: ToolExecutionComponent | None = None

    def set_spinner(self, visible: bool) -> None:
        self._show_spinner = visible

    def set_model_picker(self, picker: ModelPickerComponent | None) -> None:
        self._model_picker = picker
        if picker is not None:
            self._login_picker = None
            self._api_key_input = None
            self._settings_picker = None

    def set_settings_picker(self, picker: SettingsPickerComponent | None) -> None:
        self._settings_picker = picker
        if picker is not None:
            self._model_picker = None
            self._scoped_models = None
            self._login_picker = None
            self._auth_type = None
            self._api_key_input = None

    def set_scoped_models(self, comp: ScopedModelsSelectorComponent | None) -> None:
        self._scoped_models = comp
        if comp is not None:
            self._model_picker = None
            self._settings_picker = None
            self._login_picker = None
            self._auth_type = None
            self._api_key_input = None

    def set_login_picker(self, picker: LoginPickerComponent | None) -> None:
        self._login_picker = picker
        if picker is not None:
            self._model_picker = None
            self._auth_type = None
            self._api_key_input = None

    def set_auth_type(self, comp: AuthTypeComponent | None) -> None:
        self._auth_type = comp
        if comp is not None:
            self._model_picker = None
            self._login_picker = None
            self._api_key_input = None

    def set_api_key_input(self, inp: ApiKeyInputComponent | None) -> None:
        self._api_key_input = inp
        if inp is not None:
            self._model_picker = None
            self._login_picker = None
            self._auth_type = None

    def set_tree_selector(self, comp: TreeSelectorComponent | None) -> None:
        self._tree_selector = comp
        if comp is not None:
            self._model_picker = None
            self._settings_picker = None
            self._scoped_models = None
            self._login_picker = None
            self._auth_type = None
            self._api_key_input = None

    def clear_overlays(self) -> None:
        self._model_picker = None
        self._settings_picker = None
        self._scoped_models = None
        self._login_picker = None
        self._auth_type = None
        self._api_key_input = None
        self._tree_selector = None

    @property
    def has_overlay(self) -> bool:
        return (
            self._model_picker is not None
            or self._settings_picker is not None
            or self._scoped_models is not None
            or self._login_picker is not None
            or self._auth_type is not None
            or self._api_key_input is not None
            or self._tree_selector is not None
        )

    def render(self, width: int) -> list[str]:
        lines: list[str] = []

        if self._current_tool:
            lines.extend(self._current_tool.render(width))
        if self._streaming:
            lines.extend(self._streaming.render(width))
        if self._show_spinner:
            lines.extend(self._spinner.render(width))

        lines.append("")

        if self._slash_picker.is_open():
            lines.extend(self._slash_picker.render(width))
        elif self._at_picker.is_open() and not self._slash_picker.is_open():
            lines.extend(self._at_picker.render(width))

        if self._tree_selector:
            lines.extend(self._tree_selector.render(width))
        elif self._model_picker:
            lines.extend(self._model_picker.render(width))
        elif self._settings_picker:
            lines.extend(self._settings_picker.render(width))
        elif self._scoped_models:
            lines.extend(self._scoped_models.render(width))
        elif self._login_picker:
            lines.extend(self._login_picker.render(width))
        elif self._auth_type:
            lines.extend(self._auth_type.render(width))
        elif self._api_key_input:
            lines.extend(self._api_key_input.render(width))
        else:
            lines.extend(self._editor.render(width))

        lines.append("")
        lines.extend(self._footer.render(width))

        return lines

    def invalidate(self) -> None:
        self._spinner.invalidate()
        self._editor.invalidate()
        self._footer.invalidate()
        self._slash_picker.invalidate()
        self._at_picker.invalidate()
        if self._model_picker:
            self._model_picker.invalidate()
        if self._settings_picker:
            self._settings_picker.invalidate()
        if self._scoped_models:
            self._scoped_models.invalidate()
        if self._login_picker:
            self._login_picker.invalidate()
        if self._auth_type:
            self._auth_type.invalidate()
        if self._api_key_input:
            self._api_key_input.invalidate()
        if self._tree_selector:
            self._tree_selector.invalidate()
        if self._streaming:
            self._streaming.invalidate()
        if self._current_tool:
            self._current_tool.invalidate()


class NuuApp:
    """Pi-style nuu TUI application."""

    def __init__(self, session: AgentSession) -> None:
        self.session = session
        self._is_busy = False
        self._last_assistant_text = ""
        model = session.agent.model
        self._model_label = f"{model.provider}/{model.id}"
        self._cwd = os.getcwd()

        # Components
        self._spinner = SpinnerComponent("Thinking")
        self._slash_picker = SlashPickerComponent(_SLASH_ENTRIES)
        self._at_picker = AtPickerComponent(cwd=self._cwd)
        self._footer = FooterComponent(self._model_label, self._cwd)
        self._editor = Editor(self._model_label)

        self._bottom = _BottomComponent(
            self._spinner,
            self._slash_picker,
            self._at_picker,
            self._editor,
            self._footer,
        )

        # Wire editor callbacks
        self._editor.on_submit = self._on_submit
        self._editor.on_interrupt = self._on_interrupt
        self._editor.on_escape = self._on_escape
        self._editor.on_ctrl_d = self._on_ctrl_d

        # TUI engine + running event loop (set in run())
        self._tui: TUI | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Background tasks
        self._spinner_task: asyncio.Task | None = None
        self._session_name: str = ""
        self._last_interrupt_time: float = 0.0
        self._pending_message: str | None = None

        # Scoped models for Ctrl+P cycling (loaded from settings)
        self._scoped_models: list[Any] = self._load_scoped_models()

        # Seed footer with current model's context window + git branch + thinking level
        self._footer.set_context_window(getattr(session.agent.model, "context_window", 0))
        self._footer.set_git_branch(self._read_git_branch())
        self._sync_footer_thinking()

        # Last tool output for Ctrl+O expand/collapse
        self._last_tool_item: ScrollbackItem | None = None
        self._last_tool_component: Any = None

        # Sync hide_thinking class flag from settings
        self._sync_hide_thinking()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.session.subscribe(self._on_agent_event)

        async with TUI() as tui:
            self._tui = tui
            tui.set_bottom(self._bottom)
            tui.set_input_handler(self._handle_input)

            # Emit welcome header into scroll history on first render
            w = tui.get_width()
            border = ansi.fg(theme._resolve("borderMuted"), "─" * w)
            title = theme.fg("accent", "nuu") + theme.fg("dim", f"  {self._cwd}")
            tui.emit([border, title, border])

            await tui.run()

    # ------------------------------------------------------------------
    # Emit helper
    # ------------------------------------------------------------------

    def _emit_message(self, component: Any) -> "ScrollbackItem | None":
        if self._tui:
            self._tui.emit([""])
            return self._tui.emit_component(component)
        return None

    # ------------------------------------------------------------------
    # Input routing
    # ------------------------------------------------------------------

    def _handle_input(self, data: str) -> None:
        log.debug("input: %r", data)
        from .engine.keys import is_key_release

        if is_key_release(data):
            return
        from .engine.keybindings import get_keybindings

        kb = get_keybindings()

        # Ctrl+C must work even when editor is disabled (busy)
        if kb.matches(data, "tui.input.copy"):
            self._on_interrupt()
            if self._tui:
                self._tui.request_render()
            return

        # Escape while busy: abort agent
        if kb.matches(data, "tui.select.cancel") and self._is_busy:
            self._on_escape()
            if self._tui:
                self._tui.request_render()
            return

        # Overlay active — route all input to the active overlay
        if self._bottom._api_key_input:
            self._bottom._api_key_input.handle_input(data)
            if self._tui:
                self._tui.request_render()
            return
        if self._bottom._auth_type:
            self._bottom._auth_type.handle_input(data)
            if self._tui:
                self._tui.request_render()
            return
        if self._bottom._login_picker:
            self._bottom._login_picker.handle_input(data)
            if self._tui:
                self._tui.request_render()
            return
        if self._bottom._model_picker:
            self._bottom._model_picker.handle_input(data)
            if self._tui:
                self._tui.request_render()
            return
        if self._bottom._settings_picker:
            self._bottom._settings_picker.handle_input(data)
            if self._tui:
                self._tui.request_render()
            return
        if self._bottom._scoped_models:
            self._bottom._scoped_models.handle_input(data)
            if self._tui:
                self._tui.request_render()
            return
        if self._bottom._tree_selector:
            self._bottom._tree_selector.handle_input(data)
            if self._tui:
                self._tui.request_render()
            return

        # Ctrl+P / Shift+Ctrl+P — cycle model
        if kb.matches(data, "app.model.cycleForward"):
            self._cycle_model("forward")
            if self._tui:
                self._tui.request_render()
            return
        if kb.matches(data, "app.model.cycleBackward"):
            self._cycle_model("backward")
            if self._tui:
                self._tui.request_render()
            return

        # Ctrl+O — expand/collapse last tool output
        if kb.matches(data, "app.tool.toggleExpand"):
            self._toggle_tool_expand()
            if self._tui:
                self._tui.request_render()
            return

        # Ctrl+T — toggle thinking block visibility
        if kb.matches(data, "app.thinking.toggle"):
            self._toggle_thinking_visibility()
            if self._tui:
                self._tui.request_render()
            return

        # Shift+Tab — cycle thinking level
        if kb.matches(data, "app.thinking.cycle"):
            self._cycle_thinking_level()
            if self._tui:
                self._tui.request_render()
            return

        # @-picker active — route nav keys (before slash picker)
        if self._bottom._at_picker.is_open():
            if kb.matches(data, "tui.select.up"):
                self._bottom._at_picker.move_up()
                if self._tui:
                    self._tui.request_render()
                return
            if kb.matches(data, "tui.select.down"):
                self._bottom._at_picker.move_down()
                if self._tui:
                    self._tui.request_render()
                return
            if kb.matches(data, "tui.select.confirm") or kb.matches(
                data, "tui.input.tab"
            ):
                match = self._bottom._at_picker.confirm()
                if match:
                    # Prepend @ since it's being replaced (the token starts with @)
                    insert_text = f"@{match.insert}"
                    self._editor.insert_text_at_cursor(insert_text)
                    # Add a space after file completion (unless it's a directory)
                    if not match.is_directory:
                        self._editor._insert(' ')
                        self._bottom._at_picker.hide()
                    else:
                        # For directories, keep the picker open with the new prefix
                        self._update_at_picker()
                else:
                    self._bottom._at_picker.hide()
                if self._tui:
                    self._tui.request_render()
                return
            if kb.matches(data, "tui.select.cancel"):
                self._bottom._at_picker.hide()
                if self._tui:
                    self._tui.request_render()
                return
            # If user keeps typing, forward to editor and update
            self._editor.handle_input(data)
            self._update_at_picker()
            if self._tui:
                self._tui.request_render()
            return

        # Slash picker active — route nav keys
        if self._slash_picker.is_open():
            if kb.matches(data, "tui.select.up"):
                self._slash_picker.move_up()
                if self._tui:
                    self._tui.request_render()
                return
            if kb.matches(data, "tui.select.down"):
                self._slash_picker.move_down()
                if self._tui:
                    self._tui.request_render()
                return
            if kb.matches(data, "tui.select.confirm") or kb.matches(
                data, "tui.input.tab"
            ):
                cmd = self._slash_picker.confirm()
                self._editor.clear()
                if cmd:
                    self._dispatch_slash(cmd, "")
                if self._tui:
                    self._tui.request_render()
                return
            if kb.matches(data, "tui.select.cancel"):
                self._slash_picker.hide()
                if self._tui:
                    self._tui.request_render()
                return

        # Forward to editor
        self._editor.handle_input(data)

        # Update slash picker based on current text
        text = self._editor.text
        if text.startswith("/") and "\n" not in text:
            matches = self._slash_picker.filter_commands(text[1:])
            if matches:
                self._slash_picker.show(matches)
            else:
                self._slash_picker.hide()
        else:
            self._slash_picker.hide()

        # Update @-picker based on cursor position
        self._update_at_picker()

        if self._tui:
            self._tui.request_render()

    def _update_at_picker(self) -> None:
        """Check editor for @-prefix and update the picker."""
        if self._is_busy:
            self._bottom._at_picker.hide()
            return
        at_prefix = self._editor.extract_at_prefix()
        if at_prefix and len(at_prefix) > 1:
            # The @ prefix includes the @ sign; pass just the query part
            query = at_prefix[1:]  # remove the @
            self._bottom._at_picker.update(query)
        else:
            self._bottom._at_picker.hide()

    def _on_submit(self, text: str) -> None:
        self._slash_picker.hide()
        self._bottom._at_picker.hide()
        self._editor.clear()
        if text.startswith("/"):
            rest = text[1:].strip()
            parts = shlex.split(rest) if rest else []
            cmd = parts[0].lower() if parts else ""
            args = " ".join(parts[1:]) if len(parts) > 1 else ""
            self._dispatch_slash(cmd, args)
        else:
            asyncio.ensure_future(self._start_prompt(text))
        if self._tui:
            self._tui.request_render()

    def _on_interrupt(self) -> None:
        if self._is_busy:
            log.info("interrupt: aborting agent")
            self.session.agent.abort()
            return
        import time

        now = time.monotonic()
        if now - self._last_interrupt_time < 0.5:
            if self._tui:
                self._tui.stop()
        else:
            self._last_interrupt_time = now
            self._editor.clear()
            if self._tui:
                self._tui.request_render()

    def _on_escape(self) -> None:
        if self._bottom.has_overlay:
            self._bottom.clear_overlays()
            if self._tui:
                self._tui.request_render()
            return
        if self._is_busy:
            log.info("escape: aborting agent")
            self.session.agent.abort()
            return
        # When idle: do nothing — Escape does not quit (matches Pi behavior)

    def _on_ctrl_d(self) -> None:
        # Only called when editor is empty (enforced by Editor.handle_input)
        log.info("ctrl+d: quitting")
        if self._tui:
            self._tui.stop()

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    def _dispatch_slash(self, cmd: str, args: str = "") -> None:
        match cmd:
            case "quit" | "exit":
                if self._tui:
                    self._tui.stop()
                return
            case "new":
                self._last_assistant_text = ""
                self._emit_message(SystemMessageComponent("Session cleared."))
            case "model":
                if "/" in args:
                    from ..ai.models import get_model

                    parts = args.split("/", 1)
                    model = get_model(parts[0].strip(), parts[1].strip())
                    if model:
                        self._on_model_selected(model)
                        return
                picker = ModelPickerComponent(
                    current=self.session.agent.model,
                    initial_query=args,
                )
                picker.on_select = self._on_model_selected
                picker.on_cancel = self._on_model_cancel
                self._bottom.set_model_picker(picker)
            case "copy":
                asyncio.ensure_future(self._copy_last_message())
                return
            case "session":
                self._show_session_info()
            case "login":
                self._show_login_picker()
            case "logout":
                self._show_logout_picker()
            case "export":
                self._handle_export(args)
            case "import":
                self._handle_import(args)
            case "name":
                self._handle_name(args)
            case "settings":
                self._handle_settings(args)
            case "compact":
                self._handle_compact(args)
            case "reload":
                self._handle_reload()
            case "resume":
                self._handle_resume()
            case "hotkeys":
                self._handle_hotkeys()
            case "changelog":
                self._handle_changelog()
            case "scoped-models":
                self._handle_scoped_models()
            case "share":
                self._handle_share()
            case "fork":
                self._handle_fork(args)
            case "clone":
                self._handle_clone(args)
            case "tree":
                self._handle_tree()
            case "debug":
                self._handle_debug()
            case _:
                if cmd:
                    self._emit_message(
                        SystemMessageComponent(f"/{cmd}: unknown command.")
                    )
        if self._tui:
            self._tui.request_render()

    def _on_model_cancel(self) -> None:
        self._bottom.set_model_picker(None)
        if self._tui:
            self._tui.request_render()

    def _on_model_selected(self, model: Any) -> None:
        self.session.agent.model = model
        self._model_label = f"{model.provider}/{model.id}"
        self._footer.set_model(self._model_label)
        self._footer.set_context_window(getattr(model, "context_window", 0))
        self._editor.set_model_label(self._model_label)
        self._bottom.set_model_picker(None)
        self._persist_model(model)
        self._sync_footer_thinking()
        self._emit_message(
            SystemMessageComponent(f"Model switched to {self._model_label}.")
        )
        if self._tui:
            self._tui.request_render()

    def _persist_model(self, model: Any) -> None:
        try:
            from ..coding_agent.config import get_settings_file
            from ..coding_agent.core.settings_manager import SettingsManager

            sf = get_settings_file()
            sm = SettingsManager(sf)
            sm.set("default_provider", model.provider)
            sm.set("default_model", model.id)
            sm.save()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # /export - Export session to JSONL
    # ------------------------------------------------------------------

    def _handle_export(self, path: str) -> None:
        sm = self.session.session_manager

        path = path.strip()
        if len(path) >= 2 and path[0] in ('"', "'") and path[0] == path[-1]:
            path = path[1:-1]

        if not path:
            path = f"nuu-session-{sm.session_id[:8]}.html"

        filepath = path if os.path.isabs(path) else os.path.join(self._cwd, path)

        if filepath.endswith(".jsonl"):
            try:
                with open(filepath, "w") as f:
                    for entry in sm.entries:
                        f.write(entry.model_dump_json(by_alias=True) + "\n")
                self._emit_message(SystemMessageComponent(f"Session exported to: {filepath}"))
            except Exception as e:
                self._emit_message(ErrorMessageComponent(f"Failed to export session: {e}"))
        else:
            try:
                html = self._build_html_export()
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(html)
                self._emit_message(SystemMessageComponent(f"Session exported to: {filepath}"))
            except Exception as e:
                self._emit_message(ErrorMessageComponent(f"Failed to export HTML: {e}"))

    def _build_html_export(self) -> str:
        import html as _html
        import json as _json
        from ..coding_agent.session_manager import (
            SessionHeader, SessionMessageEntry, ModelChangeEntry,
            CompactionEntry, SessionInfoEntry,
        )

        sm = self.session.session_manager
        header_e = next((e for e in sm.entries if isinstance(e, SessionHeader)), None)
        created = header_e.timestamp if header_e else ""
        model_label = f"{self.session.agent.model.provider}/{self.session.agent.model.id}"

        def esc(t: str) -> str:
            return _html.escape(str(t))

        def render_blocks(blocks) -> str:
            parts = []
            for b in blocks:
                btype = getattr(b, "type", "")
                if btype == "text":
                    text = getattr(b, "text", "") or ""
                    if text.strip():
                        parts.append(f'<pre class="text">{esc(text)}</pre>')
                elif btype == "thinking":
                    t = getattr(b, "thinking", "") or ""
                    if t.strip():
                        parts.append(f'<pre class="thinking">{esc(t)}</pre>')
                elif btype == "toolCall":
                    name = getattr(b, "name", "") or ""
                    args = getattr(b, "arguments", {}) or {}
                    astr = _json.dumps(args, indent=2) if args else ""
                    parts.append(
                        f'<div class="tool-call"><span class="tname">{esc(name)}</span>'
                        + (f'<pre class="targs">{esc(astr)}</pre>' if astr else "")
                        + "</div>"
                    )
                elif btype == "toolResult":
                    tc = getattr(b, "content", []) or []
                    txt = "\n".join(getattr(c, "text", "") for c in tc if getattr(c, "text", ""))
                    if txt.strip():
                        truncated = txt[:3000]
                        suffix = "\n...(truncated)" if len(txt) > 3000 else ""
                        parts.append(f'<pre class="tool-result">{esc(truncated)}{suffix}</pre>')
            return "\n".join(parts)

        msgs_html: list[str] = []
        for entry in sm.entries:
            if isinstance(entry, SessionHeader):
                continue
            if isinstance(entry, ModelChangeEntry):
                msgs_html.append(
                    f'<div class="meta">Model → {esc(entry.provider)}/{esc(entry.model_id)}</div>'
                )
            elif isinstance(entry, CompactionEntry):
                msgs_html.append(
                    f'<div class="meta compacted">Compacted ({esc(str(entry.tokens_before))} msgs)</div>'
                )
            elif isinstance(entry, SessionInfoEntry):
                if entry.name:
                    msgs_html.append(f'<div class="meta">Name: {esc(entry.name)}</div>')
            elif isinstance(entry, SessionMessageEntry):
                role = entry.message.role
                if role == "toolResult":
                    continue
                content_html = render_blocks(entry.message.content or [])
                if not content_html.strip():
                    continue
                role_label = "You" if role == "user" else "Assistant"
                role_cls = "user" if role == "user" else "assistant"
                msgs_html.append(
                    f'<div class="msg {role_cls}">'
                    f'<div class="msg-hdr"><span class="role">{role_label}</span>'
                    + (f'<span class="ts">{esc(entry.timestamp)}</span>' if entry.timestamp else "")
                    + f"</div>"
                    f'<div class="msg-body">{content_html}</div>'
                    "</div>"
                )

        css = (
            "body{background:#1a1a1a;color:#d4d4d4;font-family:'Courier New',monospace;"
            "max-width:900px;margin:0 auto;padding:20px;line-height:1.5}"
            ".hdr{border-bottom:1px solid #333;padding-bottom:12px;margin-bottom:24px;color:#888}"
            ".hdr h1{color:#6ee7b7;font-size:1.1em;margin:0 0 6px}"
            ".msg{margin:14px 0;padding:10px 14px;border-radius:4px}"
            ".msg.user{background:#2a2a2a;border-left:3px solid #a5d8ff}"
            ".msg.assistant{background:#1e1e1e;border-left:3px solid #6ee7b7}"
            ".msg-hdr{font-size:.8em;color:#666;margin-bottom:6px;display:flex;gap:10px}"
            ".msg.user .role{color:#a5d8ff;font-weight:bold}"
            ".msg.assistant .role{color:#6ee7b7;font-weight:bold}"
            ".ts{color:#555}"
            "pre{white-space:pre-wrap;word-wrap:break-word;margin:6px 0;font-size:.9em}"
            "pre.thinking{color:#888;font-style:italic;border-left:2px solid #444;padding-left:10px}"
            ".tool-call{background:#1e2a1e;border:1px solid #2a4a2a;border-radius:3px;padding:8px 12px;margin:6px 0}"
            ".tname{color:#6ee7b7;font-weight:bold;display:block;margin-bottom:3px}"
            "pre.targs{color:#a0a0a0;font-size:.85em;margin:0}"
            "pre.tool-result{background:#1e1e2a;border-left:2px solid #444;padding:6px 10px;color:#999;font-size:.85em}"
            ".meta{color:#666;font-size:.8em;margin:10px 0;padding:3px 8px;border-left:2px solid #444}"
            ".compacted{background:#222}"
        )

        return (
            '<!DOCTYPE html>\n<html lang="en">\n<head>'
            '<meta charset="UTF-8">'
            f'<title>nuu session {esc(sm.session_id[:8])}</title>'
            f"<style>{css}</style>"
            "</head>\n<body>"
            f'<div class="hdr"><h1>nuu session</h1>'
            f"<div>ID: {esc(sm.session_id)}</div>"
            f"<div>CWD: {esc(sm.cwd)}</div>"
            f"<div>Model: {esc(model_label)}</div>"
            + (f"<div>Created: {esc(created)}</div>" if created else "")
            + "</div>\n<main>"
            + "\n".join(msgs_html)
            + "</main>\n</body>\n</html>"
        )

    # ------------------------------------------------------------------
    # /import - Import session from JSONL
    # ------------------------------------------------------------------

    def _handle_import(self, path: str) -> None:
        if not path:
            self._emit_message(
                SystemMessageComponent(
                    "Usage: /import <path>  Import session from JSONL file"
                )
            )
            return
        try:
            filepath = path if os.path.isabs(path) else os.path.join(self._cwd, path)
            import json as _json

            session_id: str | None = None
            session_cwd: str | None = None
            msgs = 0
            total = 0
            with open(filepath) as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    total += 1
                    data = _json.loads(stripped)
                    entry_type = data.get("type")
                    if entry_type == "session":
                        session_id = data.get("id")
                        session_cwd = data.get("cwd")
                    elif entry_type == "message":
                        msgs += 1
            lines = [
                f"Imported {total} entries ({msgs} messages) from:",
                f"  {filepath}",
            ]
            if session_id:
                lines.append(f"  Session: {session_id}")
                if session_cwd:
                    lines.append(f"  CWD    : {session_cwd}")
            lines.append(
                "Use /resume to list sessions, or start a new session with /new"
            )
            self._emit_message(SystemMessageComponent("\n".join(lines)))
        except FileNotFoundError:
            self._emit_message(ErrorMessageComponent(f"File not found: {filepath}"))
        except Exception as e:
            self._emit_message(ErrorMessageComponent(f"Import failed: {e}"))

    # ------------------------------------------------------------------
    # /name - Set or display session name
    # ------------------------------------------------------------------

    def _handle_name(self, name: str) -> None:
        if name:
            self._session_name = name
            self._footer.set_session_name(name)
            self._emit_message(SystemMessageComponent(f"Session name set: {name}"))
        else:
            if self._session_name:
                self._emit_message(
                    SystemMessageComponent(f"Session name: {self._session_name}")
                )
            else:
                self._emit_message(SystemMessageComponent("Usage: /name <name>"))

    # ------------------------------------------------------------------
    # /settings - Show current settings
    # ------------------------------------------------------------------

    def _handle_settings(self, _key: str = "") -> None:
        try:
            from ..coding_agent.config import get_settings_file
            from ..coding_agent.core.settings_manager import SettingsManager

            sf = get_settings_file()
            sm = SettingsManager(sf)
            picker = SettingsPickerComponent(sm)
            picker.on_select = self._on_setting_changed
            picker.on_cancel = self._on_settings_cancel
            self._bottom.set_settings_picker(picker)
        except Exception as e:
            self._emit_message(ErrorMessageComponent(f"Settings: {e}"))

    def _on_setting_changed(self, key: str, value: Any) -> None:
        labels = {
            "auto_compact": "Auto-compact",
            "steering_mode": "Steering mode",
            "followup_mode": "Follow-up mode",
            "transport": "Transport",
            "hide_thinking": "Hide thinking",
            "collapse_changelog": "Collapse changelog",
            "default_provider": "Default provider",
            "default_model": "Default model",
        }
        label = labels.get(key, key)
        self._emit_message(SystemMessageComponent(f"Setting {label} → {value}"))
        if key == "hide_thinking":
            from .widgets.messages import AssistantMessageComponent
            AssistantMessageComponent.set_hide_thinking(bool(value))
            if self._tui:
                self._tui.invalidate_scrollback()
        if self._tui:
            self._tui.request_render()

    def _read_git_branch(self) -> str | None:
        try:
            import subprocess
            result = subprocess.run(
                ["git", "-C", self._cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=2,
            )
            branch = result.stdout.strip()
            return branch if branch and branch != "HEAD" else None
        except Exception:
            return None

    def _sync_hide_thinking(self) -> None:
        try:
            from ..coding_agent.config import get_settings_file
            from ..coding_agent.core.settings_manager import SettingsManager
            from .widgets.messages import AssistantMessageComponent
            sm = SettingsManager(get_settings_file())
            AssistantMessageComponent.set_hide_thinking(bool(sm.get("hide_thinking", False)))
        except Exception:
            pass

    def _toggle_thinking_visibility(self) -> None:
        from .widgets.messages import AssistantMessageComponent
        new_val = not AssistantMessageComponent._hide_thinking
        AssistantMessageComponent.set_hide_thinking(new_val)
        if self._tui:
            self._tui.invalidate_scrollback()
        try:
            from ..coding_agent.config import get_settings_file
            from ..coding_agent.core.settings_manager import SettingsManager
            sm = SettingsManager(get_settings_file())
            sm.set("hide_thinking", new_val)
            sm.save()
        except Exception:
            pass
        label = "hidden" if new_val else "visible"
        self._emit_message(SystemMessageComponent(f"Thinking blocks: {label}"))

    def _toggle_tool_expand(self) -> None:
        if self._last_tool_component and self._last_tool_item:
            self._last_tool_component.toggle_expand()
            self._last_tool_item.invalidate()
            if self._tui:
                self._tui.request_render()

    def _sync_footer_thinking(self) -> None:
        model = self.session.agent.model
        supports = getattr(model, "reasoning", False)
        level = self.session.agent.thinking_level if supports else None
        self._footer.set_thinking_level(level, bool(supports))

    def _cycle_thinking_level(self) -> None:
        from ..ai.models import get_supported_thinking_levels
        model = self.session.agent.model
        if not getattr(model, "reasoning", False):
            self._emit_message(SystemMessageComponent("Current model does not support thinking."))
            return
        available = get_supported_thinking_levels(model)
        current = self.session.agent.thinking_level
        try:
            idx = available.index(current)
        except ValueError:
            idx = 0
        new_level = available[(idx + 1) % len(available)]
        self.session.agent.thinking_level = new_level
        self._sync_footer_thinking()
        self._emit_message(SystemMessageComponent(f"Thinking: {new_level}"))

    def _on_settings_cancel(self) -> None:
        self._bottom.set_settings_picker(None)
        if self._tui:
            self._tui.request_render()

    # ------------------------------------------------------------------
    # /compact - Trigger manual compaction
    # ------------------------------------------------------------------

    def _handle_compact(self, instructions: str = "") -> None:
        if self._is_busy:
            self._emit_message(SystemMessageComponent("Cannot compact while agent is running."))
            return
        messages = self.session.agent.messages
        if not messages:
            self._emit_message(SystemMessageComponent("Nothing to compact — no messages yet."))
            return
        asyncio.ensure_future(self._run_compact(instructions.strip()))

    async def _run_compact(self, instructions: str = "") -> None:
        from ..coding_agent.compaction import generate_summary
        from ..coding_agent.session import _resolve_api_key
        from ..coding_agent.session_manager import CompactionEntry
        from ..ai.types import UserMessage, TextContent
        import time as _time

        try:
            model = self.session.agent.model
            api_key = _resolve_api_key(model.provider)
            if not api_key:
                self._emit_message(ErrorMessageComponent(
                    f"No API key found for {model.provider}. Use /login to configure."
                ))
                return

            messages = list(self.session.agent.messages)
            msg_count = len(messages)
            self._emit_message(SystemMessageComponent(f"Compacting {msg_count} messages..."))
            if self._tui:
                self._tui.request_render()

            thinking_level = getattr(self.session.agent, "thinking_level", "off")
            summary = await generate_summary(messages, model, api_key, thinking_level)

            if instructions:
                summary = f"{instructions}\n\n{summary}"

            # Replace agent messages with a single summary message
            summary_msg = UserMessage(
                role="user",
                content=[TextContent(
                    type="text",
                    text=f"<compaction-summary>\n{summary}\n</compaction-summary>",
                )],
                timestamp=int(_time.time() * 1000),
            )
            self.session.agent.messages = [summary_msg]

            # Persist a CompactionEntry
            sm = self.session.session_manager
            entry = CompactionEntry(
                id=sm._generate_id(),
                parent_id=sm.leaf_id,
                timestamp=sm._now_iso(),
                summary=summary,
                first_kept_entry_id=sm.leaf_id or "",
                tokens_before=msg_count,
            )
            sm._append_entry(entry)

            self._emit_message(SystemMessageComponent(
                f"Compacted {msg_count} messages into summary ({len(summary)} chars)."
            ))
        except Exception as e:
            self._emit_message(ErrorMessageComponent(f"Compact failed: {e}"))
        if self._tui:
            self._tui.request_render()

    # ------------------------------------------------------------------
    # /reload - Reload resources
    # ------------------------------------------------------------------

    def _handle_reload(self) -> None:
        reloaded: list[str] = []
        errors: list[str] = []

        try:
            from ..coding_agent.config import get_settings_file
            from ..coding_agent.core.settings_manager import SettingsManager
            from .widgets.messages import AssistantMessageComponent as _AMC
            sm = SettingsManager(get_settings_file())
            new_hide = bool(sm.get("hide_thinking", False))
            if new_hide != _AMC._hide_thinking:
                _AMC.set_hide_thinking(new_hide)
                if self._tui:
                    self._tui.invalidate_scrollback()
            reloaded.append("settings")
        except Exception as e:
            msg = f"settings: {e}"
            log.warning("reload %s", msg)
            errors.append(msg)

        try:
            from .engine.keybindings import (
                KeybindingsManager, TUI_KEYBINDINGS, set_keybindings
            )
            from ..coding_agent.config import get_settings_file
            from ..coding_agent.core.settings_manager import SettingsManager
            sm = SettingsManager(get_settings_file())
            user_kb = sm.get("keybindings") or {}
            set_keybindings(KeybindingsManager(TUI_KEYBINDINGS, user_kb))
            reloaded.append("keybindings")
        except Exception as e:
            msg = f"keybindings: {e}"
            log.warning("reload %s", msg)
            errors.append(msg)

        try:
            self._scoped_models = self._load_scoped_models()
            reloaded.append("scoped models")
        except Exception as e:
            msg = f"scoped models: {e}"
            log.warning("reload %s", msg)
            errors.append(msg)

        try:
            self._footer.set_git_branch(self._read_git_branch())
            reloaded.append("git branch")
        except Exception as e:
            msg = f"git branch: {e}"
            log.warning("reload %s", msg)
            errors.append(msg)

        self._sync_footer_thinking()

        if reloaded:
            self._emit_message(SystemMessageComponent(
                "Reloaded: " + ", ".join(reloaded)
            ))
        if errors:
            self._emit_message(ErrorMessageComponent(
                "Reload errors: " + "; ".join(errors)
            ))
        if not reloaded and not errors:
            self._emit_message(SystemMessageComponent(
                "Nothing to reload."
            ))

    # ------------------------------------------------------------------
    # /resume - List available sessions
    # ------------------------------------------------------------------

    def _handle_resume(self) -> None:
        from ..coding_agent.config import get_sessions_dir
        import json as _json
        from datetime import datetime

        sess_dir = get_sessions_dir()
        if not sess_dir.exists():
            self._emit_message(SystemMessageComponent("No sessions directory found."))
            return
        try:
            files = sorted(
                sess_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except OSError:
            self._emit_message(
                SystemMessageComponent("Error reading sessions directory.")
            )
            return
        if not files:
            self._emit_message(SystemMessageComponent("No saved sessions found."))
            return
        lines: list[str] = [f"Sessions ({sess_dir}):", ""]
        count = 0
        for f in files:
            try:
                if f.stat().st_size == 0:
                    continue
                with open(f) as fh:
                    header_line = fh.readline().strip()
                    if not header_line:
                        continue
                    header = _json.loads(header_line)
                    if header.get("type") != "session":
                        continue

                    msg_count = 0
                    model_info: str | None = None
                    for line in fh:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        data = _json.loads(stripped)
                        etype = data.get("type")
                        if etype == "message":
                            msg_count += 1
                        elif etype == "model_change" and model_info is None:
                            prov = data.get("provider", "")
                            mid = data.get("modelId", "")
                            if prov and mid:
                                model_info = f"{prov}/{mid}"

                    timestamp = header.get("timestamp", "")
                    date_str = ""
                    if timestamp:
                        try:
                            dt = datetime.fromisoformat(timestamp)
                            date_str = dt.strftime("%Y-%m-%d")
                        except (ValueError, TypeError):
                            date_str = timestamp[:10] if len(timestamp) >= 10 else ""

                    name = f.stem
                    parts = [f"{name} ({msg_count} messages"]
                    if date_str:
                        parts.append(date_str)
                    if model_info:
                        parts.append(model_info)
                    parts[-1] += ")"
                    label = ", ".join(parts) if len(parts) > 1 else parts[0]
                    lines.append(f"  {count + 1}. {label}")
                    count += 1
            except OSError:
                continue
            except _json.JSONDecodeError:
                continue
            if count >= 20:
                break
        remaining = len(files) - count
        if remaining > 0:
            lines.append(f"  ... and {remaining} more")
        if count == 0:
            lines.append("  (no valid session files found)")
        lines.append("")
        lines.append("Use /import <path> to load a session file.")
        self._emit_message(SystemMessageComponent("\n".join(lines)))

    # ------------------------------------------------------------------
    # /hotkeys - Show keyboard shortcuts
    # ------------------------------------------------------------------

    def _handle_hotkeys(self) -> None:
        from ..coding_agent.slash_commands import BUILTIN_SLASH_COMMANDS

        navigation = [
            "  Up/Down       Navigate lists (slash picker, model/login selectors)",
            "  Tab           Confirm slash picker selection",
            "  Escape        Close overlay / Abort agent (when busy)",
        ]
        editor_keys = [
            "  Enter         Submit message",
            "  Shift+Enter   New line in editor",
            "  Ctrl+C        Abort agent (busy) / Clear editor; press twice to quit",
            "  Ctrl+D        Quit (when editor is empty)",
            "  Ctrl+-        Undo",
            "  Ctrl+K        Kill to line end (adds to kill ring)",
            "  Ctrl+U        Kill to line start (adds to kill ring)",
            "  Ctrl+W        Kill word backward (adds to kill ring)",
            "  Alt+D         Kill word forward (adds to kill ring)",
            "  Ctrl+Y        Yank (paste from kill ring)",
            "  Alt+Y         Yank-pop (cycle kill ring after yank)",
            "  Ctrl+]        Jump forward to char (type char after)",
            "  Ctrl+Alt+]    Jump backward to char (type char after)",
            "  Alt+Left/Right  Move word left/right",
            "  Ctrl+A        Move to line start",
            "  Ctrl+E        Move to line end",
            "  Ctrl+P        Cycle to next model",
            "  Shift+Ctrl+P  Cycle to previous model",
            "  Ctrl+O        Expand/collapse last tool output",
            "  Ctrl+T        Toggle thinking block visibility",
            "  Shift+Tab     Cycle thinking level (reasoning models only)",
        ]
        slash_cmds = [
            f"  /{cmd.name:<20} {cmd.description}" for cmd in BUILTIN_SLASH_COMMANDS
        ]

        keys = [
            "Keyboard shortcuts",
            "━━━━━━━━━━━━━━━━━━━",
            "",
            "Navigation:",
            *navigation,
            "",
            "Editor:",
            *editor_keys,
            "",
            "Slash commands:",
            *slash_cmds,
        ]
        self._emit_message(SystemMessageComponent("\n".join(keys)))

    # ------------------------------------------------------------------
    # /changelog - Show version info
    # ------------------------------------------------------------------

    def _handle_changelog(self) -> None:
        from ..coding_agent.config import VERSION, APP_NAME
        from ..coding_agent.config import get_agent_dir

        lines = [
            f"{APP_NAME} v{VERSION}",
            "",
            f"  Agent dir: {get_agent_dir()}",
            f"  Python   : {sys.version.split()[0]}",
            f"  Platform : {sys.platform}",
            "",
            "See ref/pi/ (TypeScript original) for full changelog.",
        ]
        self._emit_message(SystemMessageComponent("\n".join(lines)))

    # ------------------------------------------------------------------
    # /scoped-models - Enable/disable models for Ctrl+P cycling
    # ------------------------------------------------------------------

    def _load_scoped_models(self) -> list[Any]:
        try:
            from ..coding_agent.config import get_settings_file
            from ..coding_agent.core.settings_manager import SettingsManager
            from ..ai.models import get_model

            sm = SettingsManager(get_settings_file())
            enabled = sm.get_enabled_models()
            if enabled is None:
                return []
            models = []
            for full_id in enabled:
                if "/" in full_id:
                    provider, model_id = full_id.split("/", 1)
                    m = get_model(provider, model_id)
                    if m:
                        models.append(m)
            return models
        except Exception:
            return []

    def _handle_scoped_models(self) -> None:
        from ..ai.models import get_models, get_providers
        from ..coding_agent.config import get_settings_file
        from ..coding_agent.core.settings_manager import SettingsManager

        all_models = [m for p in get_providers() for m in get_models(p)]
        sm = SettingsManager(get_settings_file())
        enabled = sm.get_enabled_models()

        comp = ScopedModelsSelectorComponent(all_models, enabled)
        comp.on_change = self._on_scoped_models_change
        comp.on_save = self._on_scoped_models_save
        comp.on_cancel = self._on_scoped_models_cancel
        self._bottom.set_scoped_models(comp)
        if self._tui:
            self._tui.request_render()

    def _on_scoped_models_change(self, enabled: list[str] | None) -> None:
        from ..ai.models import get_model

        if enabled is None:
            self._scoped_models = []
        else:
            self._scoped_models = [
                m for full_id in enabled
                if "/" in full_id
                for m in [get_model(*full_id.split("/", 1))]
                if m
            ]

    def _on_scoped_models_save(self, enabled: list[str] | None) -> None:
        self._on_scoped_models_change(enabled)
        try:
            from ..coding_agent.config import get_settings_file
            from ..coding_agent.core.settings_manager import SettingsManager

            sm = SettingsManager(get_settings_file())
            sm.set_enabled_models(enabled)
            sm.save()
        except Exception as e:
            log.warning("failed to save enabled models: %s", e)
        self._bottom.set_scoped_models(None)
        self._emit_message(SystemMessageComponent("Model list saved."))
        if self._tui:
            self._tui.request_render()

    def _on_scoped_models_cancel(self) -> None:
        self._bottom.set_scoped_models(None)
        if self._tui:
            self._tui.request_render()

    def _cycle_model(self, direction: str = "forward") -> None:
        from ..ai.models import get_models, get_providers
        from ..coding_agent.session import _resolve_api_key

        # Use scoped list if set; otherwise all configured models
        if self._scoped_models:
            candidates = [m for m in self._scoped_models if _resolve_api_key(m.provider)]
        else:
            candidates = [
                m for p in get_providers() for m in get_models(p) if _resolve_api_key(p)
            ]

        if len(candidates) <= 1:
            return

        current = self.session.agent.model
        try:
            idx = next(i for i, m in enumerate(candidates) if m.id == current.id and m.provider == current.provider)
        except StopIteration:
            idx = -1

        if direction == "forward":
            next_idx = (idx + 1) % len(candidates)
        else:
            next_idx = (idx - 1) % len(candidates)

        self._on_model_selected(candidates[next_idx])

    # ------------------------------------------------------------------
    # /share - Share session via gist
    # ------------------------------------------------------------------

    def _handle_share(self) -> None:
        import shutil as _shutil
        import tempfile

        if not _shutil.which("gh"):
            self._emit_message(ErrorMessageComponent(
                "GitHub CLI (gh) not found.\nInstall from https://cli.github.com, then run: gh auth login"
            ))
            return

        sm = self.session.session_manager
        tmp = tempfile.NamedTemporaryFile(
            suffix=".jsonl",
            prefix=f"nuu-{sm.session_id[:8]}-",
            delete=False,
        )
        try:
            with open(tmp.name, "w") as f:
                for entry in sm.entries:
                    f.write(entry.model_dump_json(by_alias=True) + "\n")

            result = subprocess.run(
                ["gh", "gist", "create", "--public=false",
                 "--desc", f"nuu session {sm.session_id[:8]}", tmp.name],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "Unknown error").strip()
                if "not logged in" in err.lower() or "auth" in err.lower():
                    self._emit_message(ErrorMessageComponent(
                        f"Not authenticated. Run: gh auth login\n{err}"
                    ))
                else:
                    self._emit_message(ErrorMessageComponent(f"gh gist create failed: {err}"))
            else:
                url = result.stdout.strip()
                self._emit_message(SystemMessageComponent(f"Session shared (secret gist):\n  {url}"))
        except subprocess.TimeoutExpired:
            self._emit_message(ErrorMessageComponent("Share timed out. Check your network connection."))
        except Exception as e:
            self._emit_message(ErrorMessageComponent(f"Share failed: {e}"))
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # /fork - Fork from a previous message
    # ------------------------------------------------------------------

    def _handle_fork(self, args: str = "") -> None:
        import uuid as _uuid
        import time as _time
        import json as _json
        from ..coding_agent.config import get_sessions_dir
        from ..coding_agent.session_manager import SessionHeader, SessionMessageEntry

        if not args.strip():
            self._emit_message(SystemMessageComponent(
                "Usage: /fork <n>  Fork session before message #n (1-based)\n"
                "  /fork 3  →  new session containing messages 1–2 + their responses\n"
                "Use /session to see total message count."
            ))
            return

        try:
            n = int(args.strip())
        except ValueError:
            self._emit_message(ErrorMessageComponent(f"Invalid index: {args!r} — must be a number"))
            return

        if n < 1:
            self._emit_message(ErrorMessageComponent("Message index must be ≥ 1"))
            return

        sm = self.session.session_manager

        # Collect entries up to (but not including) the nth user message
        # so the fork ends cleanly after the (n-1)th complete exchange
        entries_to_keep: list = []
        user_count = 0
        for entry in sm.entries:
            is_user = (
                isinstance(entry, SessionMessageEntry)
                and entry.message.role == "user"
            )
            if is_user:
                user_count += 1
                if user_count >= n:
                    break  # stop before this user message
            entries_to_keep.append(entry)

        if user_count < n - 1:
            total = sum(
                1 for e in sm.entries
                if isinstance(e, SessionMessageEntry) and e.message.role == "user"
            )
            self._emit_message(ErrorMessageComponent(
                f"Only {total} user message(s) in session. Cannot fork before #{n}."
            ))
            return

        new_id = str(_uuid.uuid4())
        new_ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        sess_dir = get_sessions_dir()
        sess_dir.mkdir(parents=True, exist_ok=True)
        dest = str(sess_dir / f"{new_ts.replace(':', '-')}_{new_id}.jsonl")

        try:
            with open(dest, "w") as f:
                # New header with parent reference
                new_hdr = SessionHeader(
                    id=new_id,
                    timestamp=new_ts,
                    cwd=sm.cwd,
                    parent_session=sm.session_id,
                )
                f.write(new_hdr.model_dump_json(by_alias=True) + "\n")
                for entry in entries_to_keep:
                    if isinstance(entry, SessionHeader):
                        continue
                    f.write(entry.model_dump_json(by_alias=True) + "\n")

            kept = user_count - 1 if user_count >= n else user_count
            self._emit_message(SystemMessageComponent(
                f"Forked before message #{n} ({kept} message(s) kept)\n"
                f"  {dest}\n\n"
                "Restart nuu with this session file to continue from the fork."
            ))
        except Exception as e:
            self._emit_message(ErrorMessageComponent(f"Fork failed: {e}"))

    # ------------------------------------------------------------------
    # /clone - Duplicate current session
    # ------------------------------------------------------------------

    def _handle_clone(self, args: str = "") -> None:
        import uuid as _uuid
        import time as _time
        import json as _json
        from ..coding_agent.config import get_sessions_dir

        sm = self.session.session_manager
        query = args.strip()

        # Locate source file
        source: str | None = None
        if not query:
            if not sm.session_file or not os.path.exists(sm.session_file):
                self._emit_message(ErrorMessageComponent(
                    "Current session has not been saved to disk yet. "
                    "Send at least one message first."
                ))
                return
            source = sm.session_file
        else:
            sess_dir = get_sessions_dir()
            if not sess_dir.exists():
                self._emit_message(ErrorMessageComponent("No sessions directory found."))
                return
            for f in sorted(sess_dir.rglob("*.jsonl"),
                            key=lambda p: p.stat().st_mtime, reverse=True):
                if query in f.name:
                    source = str(f)
                    break
                try:
                    with open(f) as fh:
                        first = fh.readline().strip()
                        if first:
                            d = _json.loads(first)
                            if d.get("type") == "session" and query in d.get("id", ""):
                                source = str(f)
                                break
                except Exception:
                    continue
            if not source:
                self._emit_message(ErrorMessageComponent(
                    f"No session found matching: {query!r}\n"
                    "Use /resume to list available sessions."
                ))
                return

        # Write clone with a new ID
        new_id = str(_uuid.uuid4())
        new_ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        sess_dir = get_sessions_dir()
        sess_dir.mkdir(parents=True, exist_ok=True)
        dest = str(sess_dir / f"{new_ts.replace(':', '-')}_{new_id}.jsonl")

        try:
            with open(source) as src_f, open(dest, "w") as dst_f:
                for i, line in enumerate(src_f):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if i == 0:
                        d = _json.loads(stripped)
                        d["id"] = new_id
                        d["timestamp"] = new_ts
                        dst_f.write(_json.dumps(d) + "\n")
                    else:
                        dst_f.write(stripped + "\n")

            self._emit_message(SystemMessageComponent(
                f"Session cloned:\n  {dest}\n\n"
                "Restart nuu with this file to continue in the clone."
            ))
        except Exception as e:
            self._emit_message(ErrorMessageComponent(f"Clone failed: {e}"))

    # ------------------------------------------------------------------
    # /tree - Interactive session lineage selector
    # ------------------------------------------------------------------

    def _handle_tree(self) -> None:
        from ..coding_agent.config import get_sessions_dir

        sm = self.session.session_manager
        sess_dir = str(get_sessions_dir())

        # Build message list from agent.messages (what the AI actually sees)
        messages: list[dict] = []
        for i, msg in enumerate(self.session.agent.messages):
            role = getattr(msg, "role", "")
            if role not in ("user", "assistant"):
                continue
            content = getattr(msg, "content", None) or []
            if isinstance(content, list):
                text = "".join(
                    getattr(b, "text", "") for b in content
                    if getattr(b, "type", "") == "text" and getattr(b, "text", "")
                )
            elif isinstance(content, str):
                text = content
            else:
                text = ""
            preview = text.replace("\n", " ").strip()[:150]
            messages.append({"role": role, "preview": preview, "message_index": i})

        terminal_height = self._tui.get_height() if self._tui else 40
        comp = TreeSelectorComponent(
            current_id=sm.session_id,
            current_messages=messages,
            sessions_dir=sess_dir,
            terminal_height=terminal_height,
        )
        comp.on_select = self._on_tree_selected
        comp.on_cancel = self._on_tree_cancel
        self._bottom.set_tree_selector(comp)
        if self._tui:
            self._tui.request_render()

    def _on_tree_selected(self, node: TreeNode) -> None:
        self._bottom.set_tree_selector(None)

        if node.is_current_session:
            if node.is_leaf:
                # Already at the end — just close
                if self._tui:
                    self._tui.request_render()
                return
            # Navigate in-place: slice agent.messages up to (and including) this entry
            idx = node.message_index
            if idx >= 0:
                role = node.node_type
                # For user messages: go BEFORE that message (exclude it) so user can re-send
                # For assistant messages: go AFTER that response (include it) as a continuation point
                if role == "user":
                    new_messages = list(self.session.agent.messages[:idx])
                else:
                    new_messages = list(self.session.agent.messages[:idx + 1])
                self.session.agent.messages = new_messages
                n = len(new_messages)
                self._emit_message(SystemMessageComponent(
                    f"Navigated to message {idx + 1} — {n} message(s) in context."
                ))
            if self._tui:
                self._tui.request_render()
            return

        # Other session (ancestor / fork) — exit so user can restart with that file
        if node.file_path:
            self._emit_message(SystemMessageComponent(
                f"Session: {node.session_id[:8]}\n"
                f"  {node.file_path}\n\n"
                "Restart nuu with this file to load it."
            ))
        if self._tui:
            self._tui.request_render()
            self._tui.stop()

    def _on_tree_cancel(self) -> None:
        self._bottom.set_tree_selector(None)
        if self._tui:
            self._tui.request_render()

    # ------------------------------------------------------------------
    # /debug - Print debug info
    # ------------------------------------------------------------------

    def _handle_debug(self) -> None:
        import json as _json
        import platform

        agent_dir = os.path.expanduser("~/.nuu")
        info = {
            "app": {
                "cwd": self._cwd,
                "model": f"{self.session.agent.model.provider}/{self.session.agent.model.id}",
                "messages": len(self.session.agent.messages),
                "busy": self._is_busy,
                "overlay": self._bottom.has_overlay,
            },
            "session": {
                "id": self.session.session_manager.session_id,
                "file": str(self.session.session_manager.session_file or ""),
            },
            "config": {
                "agent_dir": agent_dir,
                "settings_file": os.path.join(agent_dir, "settings.json"),
                "auth_file": os.path.join(agent_dir, "auth.json"),
            },
            "system": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "node": platform.node(),
            },
        }
        self._emit_message(
            SystemMessageComponent(f"Debug info:\n{_json.dumps(info, indent=2)}")
        )

    def _show_login_picker(self) -> None:
        auth_type = AuthTypeComponent()
        auth_type.on_select = self._on_auth_type_selected
        auth_type.on_cancel = self._on_auth_type_cancel
        self._bottom.set_auth_type(auth_type)
        if self._tui:
            self._tui.request_render()

    def _on_auth_type_selected(self, auth_type: str) -> None:
        self._bottom.set_auth_type(None)
        mapped = "oauth" if auth_type == "subscription" else "api_key"
        self._show_login_providers(mapped)

    def _on_auth_type_cancel(self) -> None:
        self._bottom.set_auth_type(None)
        if self._tui:
            self._tui.request_render()

    def _show_login_providers(self, auth_type: str) -> None:
        from ..ai.env_api_keys import get_env_api_key, find_env_keys
        from ..coding_agent.core.provider_display_names import (
            BUILT_IN_PROVIDER_DISPLAY_NAMES,
        )
        from ..coding_agent.config import get_auth_file

        auth_path = get_auth_file()
        stored_keys: set[str] = set()
        if auth_path.exists():
            try:
                import json as _json
                with open(auth_path) as f:
                    stored_keys = set(_json.load(f).keys())
            except Exception:
                pass

        entries: list[tuple[str, str, str]] = []

        for pid, pname in BUILT_IN_PROVIDER_DISPLAY_NAMES.items():
            if auth_type == "oauth":
                if pid not in OAUTH_PROVIDER_IDS:
                    continue
            else:
                if pid in OAUTH_PROVIDER_IDS:
                    continue

            if pid in stored_keys:
                status = "configured"
            elif get_env_api_key(pid) or find_env_keys(pid):
                status = "env"
            else:
                status = "unconfigured"

            entries.append((pid, pname, status))

        if not entries:
            msg = (
                "No subscription providers available."
                if auth_type == "oauth"
                else "No API key providers available."
            )
            self._emit_message(SystemMessageComponent(msg))
            if self._tui:
                self._tui.request_render()
            return

        picker = LoginPickerComponent(entries, mode="login")
        picker.on_select = lambda provider_id, at=auth_type: (
            self._on_login_provider_selected(provider_id, at)
        )
        picker.on_cancel = self._on_login_cancel
        self._bottom.set_login_picker(picker)
        if self._tui:
            self._tui.request_render()

    def _on_login_provider_selected(self, provider_id: str, auth_type: str) -> None:
        self._bottom.set_login_picker(None)
        from ..coding_agent.core.provider_display_names import (
            BUILT_IN_PROVIDER_DISPLAY_NAMES,
        )

        pname = BUILT_IN_PROVIDER_DISPLAY_NAMES.get(provider_id, provider_id)

        if auth_type == "oauth":
            from ..coding_agent.core.auth_guidance import get_auth_guidance

            guidance = get_auth_guidance(provider_id)
            self._emit_message(
                SystemMessageComponent(
                    f"OAuth login for {pname}:\n\n"
                    f"{guidance}\n\n"
                    "Browser-based OAuth is not yet supported in nuu. "
                    "Set the appropriate environment variable instead."
                )
            )
            if self._tui:
                self._tui.request_render()
            return

        inp = ApiKeyInputComponent(pname)
        inp.on_submit = lambda key: self._on_api_key_submitted(provider_id, pname, key)
        inp.on_cancel = self._on_api_key_cancel
        self._bottom.set_api_key_input(inp)
        if self._tui:
            self._tui.request_render()

    def _on_api_key_submitted(
        self, provider_id: str, provider_name: str, key: str
    ) -> None:
        self._bottom.set_api_key_input(None)
        auth_dir = os.path.expanduser("~/.nuu")
        os.makedirs(auth_dir, exist_ok=True)
        auth_file = os.path.join(auth_dir, "auth.json")
        stored: dict[str, str] = {}
        if os.path.exists(auth_file):
            try:
                import json as _json

                with open(auth_file) as f:
                    stored = _json.load(f)
            except Exception:
                stored = {}
        stored[provider_id] = key
        try:
            import json as _json

            with open(auth_file, "w") as f:
                _json.dump(stored, f, indent=2)
        except Exception as e:
            self._emit_message(ErrorMessageComponent(f"Failed to save API key: {e}"))
        else:
            self._emit_message(
                SystemMessageComponent(
                    f"Saved API key for {provider_name} to {auth_file}"
                )
            )
        if self._tui:
            self._tui.request_render()

    def _on_login_cancel(self) -> None:
        self._bottom.set_login_picker(None)
        if self._tui:
            self._tui.request_render()

    def _on_api_key_cancel(self) -> None:
        self._bottom.set_api_key_input(None)
        if self._tui:
            self._tui.request_render()

    def _show_logout_picker(self) -> None:
        auth_file = os.path.expanduser("~/.nuu/auth.json")
        stored: dict[str, str] = {}
        if os.path.exists(auth_file):
            try:
                import json as _json

                with open(auth_file) as f:
                    stored = _json.load(f)
            except Exception:
                stored = {}

        if not stored:
            self._emit_message(
                SystemMessageComponent(
                    "No stored credentials to remove. /logout only removes credentials saved by /login; environment variables are unchanged."
                )
            )
            if self._tui:
                self._tui.request_render()
            return

        from ..coding_agent.core.provider_display_names import (
            BUILT_IN_PROVIDER_DISPLAY_NAMES,
        )

        entries: list[tuple[str, str, str]] = []
        for pid in stored:
            pname = BUILT_IN_PROVIDER_DISPLAY_NAMES.get(pid, pid)
            entries.append((pid, pname, "configured"))

        picker = LoginPickerComponent(entries, mode="logout")
        picker.on_select = lambda provider_id: self._on_logout_provider_selected(
            provider_id
        )
        picker.on_cancel = self._on_login_cancel
        self._bottom.set_login_picker(picker)
        if self._tui:
            self._tui.request_render()

    def _on_logout_provider_selected(self, provider_id: str) -> None:
        self._bottom.set_login_picker(None)
        auth_file = os.path.expanduser("~/.nuu/auth.json")
        stored: dict[str, str] = {}
        if os.path.exists(auth_file):
            try:
                import json as _json

                with open(auth_file) as f:
                    stored = _json.load(f)
            except Exception:
                stored = {}
        if provider_id in stored:
            del stored[provider_id]
            try:
                import json as _json

                with open(auth_file, "w") as f:
                    _json.dump(stored, f, indent=2)
            except Exception as e:
                self._emit_message(
                    ErrorMessageComponent(f"Failed to remove credentials: {e}")
                )
            else:
                from ..coding_agent.core.provider_display_names import (
                    BUILT_IN_PROVIDER_DISPLAY_NAMES,
                )

                pname = BUILT_IN_PROVIDER_DISPLAY_NAMES.get(provider_id, provider_id)
                self._emit_message(
                    SystemMessageComponent(
                        f"Removed stored API key for {pname}. Environment variables are unchanged."
                    )
                )
        else:
            self._emit_message(
                SystemMessageComponent(f"No stored credentials for {provider_id}.")
            )
        if self._tui:
            self._tui.request_render()

    # ------------------------------------------------------------------
    # Agent event bridge
    # ------------------------------------------------------------------

    def _on_agent_event(self, event: AgentEvent) -> None:
        """Called from agent thread — schedule on the TUI event loop."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._handle_agent_event, event)

    def _handle_agent_event(self, event: AgentEvent) -> None:
        log.debug("agent event: %s", type(event).__name__)

        if isinstance(event, MessageStartEvent):
            if event.message.role == "assistant":
                log.info("stream: assistant message started")
                self._bottom._streaming = AssistantMessageComponent()
            elif event.message.role == "user":
                log.debug("stream: user message")
            elif event.message.role == "toolResult":
                log.debug("stream: tool result")

        elif isinstance(event, MessageUpdateEvent):
            if self._bottom._streaming:
                ae = event.assistant_message_event
                if ae["type"] == "text_delta":
                    self._bottom._streaming.append(ae["delta"])
                elif ae["type"] == "thinking_delta":
                    self._bottom._streaming.append_thinking(ae["delta"])
                elif ae["type"] in ("text_start", "thinking_start", "toolcall_start"):
                    log.debug("stream: %s", ae["type"])

        elif isinstance(event, MessageEndEvent):
            if self._bottom._streaming and event.message.role == "assistant":
                msg = event.message
                text_parts = [
                    p.text for p in msg.content if p.type == "text" and p.text
                ]
                final_text = "".join(text_parts)

                thinking_parts = [
                    p.thinking
                    for p in msg.content
                    if p.type == "thinking" and p.thinking
                ]
                if thinking_parts:
                    self._bottom._streaming.set_thinking("".join(thinking_parts))

                streaming = self._bottom._streaming
                self._bottom._streaming = None  # remove from live panel before emitting

                if msg.stop_reason in ("error", "aborted") and msg.error_message:
                    label = "Aborted" if msg.stop_reason == "aborted" else "Error"
                    err_text = f"{label}: {msg.error_message}"
                    if (
                        "401" in msg.error_message
                        or "Unauthorized" in msg.error_message
                    ):
                        err_text += (
                            "\n\nUse /login to configure provider authentication."
                        )
                    if final_text:
                        streaming.finalize(final_text)
                        self._emit_message(streaming)
                    self._emit_message(ErrorMessageComponent(err_text))
                else:
                    streaming.finalize(final_text)
                    self._emit_message(streaming)

                self._last_assistant_text = final_text
                # Update footer token/cost stats
                if hasattr(msg, "usage") and msg.usage is not None:
                    self._footer.update_stats(msg.usage)

        elif isinstance(event, ToolExecutionStartEvent):
            log.info("tool: %s started", event.tool_name)
            self._bottom._current_tool = ToolExecutionComponent(event.tool_name, event.args)

        elif isinstance(event, ToolExecutionEndEvent):
            log.info("tool: %s done (error=%s)", event.tool_name, event.is_error)
            if self._bottom._current_tool:
                output_text = ""
                if hasattr(event, "result") and event.result is not None:
                    result = event.result
                    if hasattr(result, "content") and result.content:
                        text_parts = [
                            c.text for c in result.content if getattr(c, "text", "")
                        ]
                        output_text = "".join(text_parts)
                tool_comp = self._bottom._current_tool
                self._bottom._current_tool = None
                tool_comp.set_done(event.is_error, output_text)
                item = self._emit_message(tool_comp)
                self._last_tool_item = item
                self._last_tool_component = tool_comp

        elif isinstance(event, AgentEndEvent):
            log.info("agent: ended, error=%s", self.session.agent.error_message)
            if not self._bottom._streaming and self.session.agent.error_message:
                err = self.session.agent.error_message
                text = f"Error: {err}"
                if "401" in err or "Unauthorized" in err:
                    text += "\n\nUse /login to configure provider authentication."
                self._emit_message(ErrorMessageComponent(text))

        if self._tui:
            self._tui.request_render()

    # ------------------------------------------------------------------
    # Prompt execution
    # ------------------------------------------------------------------

    async def _start_prompt(self, text: str) -> None:
        if self._is_busy:
            self._pending_message = text
            return
        self._pending_message = None
        self._set_busy(True)
        self._emit_message(UserMessageComponent(text))
        if self._tui:
            self._tui.request_render()
        asyncio.ensure_future(self._run_prompt(text))

    async def _run_prompt(self, text: str) -> None:
        try:
            await self.session.prompt(text)
        except Exception as exc:
            log.exception("prompt error")
            self._emit_message(ErrorMessageComponent(str(exc)))
            if self._tui:
                self._tui.request_render()
        finally:
            self._set_busy(False)
            pending = self._pending_message
            if pending is not None:
                self._pending_message = None
                asyncio.ensure_future(self._start_prompt(pending))

    def _set_busy(self, busy: bool) -> None:
        self._is_busy = busy
        self._footer.set_busy(busy)
        self._bottom.set_spinner(busy)

        if busy:
            self._start_spinner_task()
        else:
            self._stop_spinner_task()

    def _start_spinner_task(self) -> None:
        if self._spinner_task and not self._spinner_task.done():
            return

        async def _spin() -> None:
            while self._is_busy:
                self._spinner.tick()
                if self._tui:
                    self._tui.request_render()
                await asyncio.sleep(0.1)

        self._spinner_task = asyncio.ensure_future(_spin())

    def _stop_spinner_task(self) -> None:
        if self._spinner_task and not self._spinner_task.done():
            self._spinner_task.cancel()
        self._spinner_task = None

    # ------------------------------------------------------------------
    # Slash helpers
    # ------------------------------------------------------------------

    async def _copy_last_message(self) -> None:
        if not self._last_assistant_text:
            self._emit_message(
                SystemMessageComponent("copy: no assistant message to copy.")
            )
            if self._tui:
                self._tui.request_render()
            return
        try:
            if sys.platform == "darwin":
                subprocess.run(
                    ["pbcopy"], input=self._last_assistant_text, text=True, check=True
                )
            else:
                r = subprocess.run(
                    ["xclip", "-sel", "clip"],
                    input=self._last_assistant_text,
                    text=True,
                )
                if r.returncode != 0:
                    subprocess.run(
                        ["xsel", "--clipboard", "--input"],
                        input=self._last_assistant_text,
                        text=True,
                        check=True,
                    )
            self._emit_message(SystemMessageComponent("Copied to clipboard."))
        except FileNotFoundError:
            self._emit_message(
                SystemMessageComponent("copy: clipboard tool not found.")
            )
        except Exception as e:
            self._emit_message(SystemMessageComponent(f"copy failed: {e}"))
        if self._tui:
            self._tui.request_render()

    def _show_session_info(self) -> None:
        from ..coding_agent.session_manager import SessionMessageEntry

        sm = self.session.session_manager
        model = self.session.agent.model
        msg_count = sum(1 for e in sm.entries if isinstance(e, SessionMessageEntry))
        lines = [
            f"Session  : {sm.session_id}",
            f"Model    : {model.provider}/{model.id}",
            f"Messages : {msg_count}",
            f"CWD      : {sm.cwd}",
        ]
        if sm.session_file:
            lines.append(f"File     : {sm.session_file}")
        self._emit_message(SystemMessageComponent("\n".join(lines)))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_tui(session: AgentSession) -> None:
    """Launch the nuu TUI and wait for it to exit."""
    _setup_logging()
    app = NuuApp(session)
    await app.run()


def _setup_logging() -> None:
    log_dir = os.path.expanduser("~/.nuu")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "debug.log")
    logging.basicConfig(
        filename=log_file,
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    log.info("nuu TUI starting, log: %s", log_file)
