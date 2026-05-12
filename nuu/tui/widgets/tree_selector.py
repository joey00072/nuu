"""Tree selector overlay — navigate conversation history (Pi-style)."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Callable, Any
from ..engine import ansi, theme


class TreeNode:
    """One row in the tree display."""

    def __init__(
        self,
        node_type: str,          # "user" | "assistant" | "tool" | "session_header"
        display_text: str,       # role-prefixed content or session summary
        raw_text: str,           # searchable text (no ANSI)
        session_id: str,
        file_path: str | None = None,
        is_current_session: bool = False,
        is_leaf: bool = False,
        message_index: int = -1, # index into agent.messages (-1 for non-message nodes)
        indent: int = 0,
        connector: str = "",     # "├─ " | "└─ " | ""
        is_on_active_path: bool = False,
    ) -> None:
        self.node_type = node_type
        self.display_text = display_text
        self.raw_text = raw_text
        self.session_id = session_id
        self.file_path = file_path
        self.is_current_session = is_current_session
        self.is_leaf = is_leaf
        self.message_index = message_index
        self.indent = indent
        self.connector = connector
        self.is_on_active_path = is_on_active_path


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _normalize(s: str) -> str:
    return s.replace("\n", " ").replace("\t", " ").strip()


def _read_session_entries(path: str) -> list[dict] | None:
    """Read messages from a session file. Returns list of {role, preview} dicts."""
    try:
        entries = []
        with open(path) as f:
            hdr = f.readline().strip()
            if not hdr:
                return None
            d = json.loads(hdr)
            if d.get("type") != "session":
                return None
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    ld = json.loads(s)
                    if ld.get("type") == "message":
                        msg = ld.get("message", {})
                        role = msg.get("role", "")
                        if role not in ("user", "assistant"):
                            continue
                        content = msg.get("content", "")
                        text = _normalize(_extract_text(content))[:150]
                        entries.append({"role": role, "preview": text or ""})
                    elif ld.get("type") == "tool_execution":
                        name = ld.get("tool_name", "tool")
                        entries.append({"role": "tool", "preview": f"[{name}]"})
                except Exception:
                    pass
        return entries
    except Exception:
        return None


def _read_session_info(path: str) -> dict | None:
    try:
        with open(path) as f:
            hdr = f.readline().strip()
            if not hdr:
                return None
            d = json.loads(hdr)
            if d.get("type") != "session":
                return None
            msgs = 0
            for line in f:
                s = line.strip()
                if s:
                    try:
                        ld = json.loads(s)
                        if (ld.get("type") == "message"
                                and ld.get("message", {}).get("role") == "user"):
                            msgs += 1
                    except Exception:
                        pass
            ts = d.get("timestamp", "")
            date = ""
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    date = dt.strftime("%Y-%m-%d")
                except Exception:
                    date = ts[:10]
            return {
                "id": d.get("id", ""),
                "parent": d.get("parent_session"),
                "date": date,
                "msgs": msgs,
                "file": path,
            }
    except Exception:
        return None


def _entry_line(role: str, preview: str) -> tuple[str, str]:
    """Return (display_text_with_color, raw_text) for a message entry."""
    if role == "user":
        colored = theme.fg("accent", "user: ") + (preview or theme.fg("dim", "(empty)"))
        raw = f"user: {preview}"
    elif role == "assistant":
        colored = theme.fg("success", "assistant: ") + (preview or theme.fg("dim", "(no content)"))
        raw = f"assistant: {preview}"
    elif role == "tool":
        colored = theme.fg("dim", preview)
        raw = preview
    else:
        colored = theme.fg("dim", f"[{role}]")
        raw = f"[{role}]"
    return colored, raw


class TreeSelectorComponent:
    """Overlay showing the session conversation tree — Pi-style."""

    # Filter modes cycling
    _FILTER_MODES = ["default", "user-only"]

    def __init__(
        self,
        current_id: str,
        current_messages: list[dict],   # [{role, preview}] from current session
        sessions_dir: str | None,
        terminal_height: int = 40,
    ) -> None:
        self._terminal_height = terminal_height
        self._current_id = current_id
        self._sel = 0
        self._nodes: list[TreeNode] = []
        self._filter_mode = "default"
        self._search_query = ""
        self._filtered: list[TreeNode] = []
        self.on_select: Callable[[TreeNode], None] | None = None
        self.on_cancel: Callable[[], None] | None = None

        self._build(current_id, current_messages, sessions_dir)
        self._apply_filter()
        # Pre-select the leaf (last message in current session)
        for i, n in enumerate(self._filtered):
            if n.is_leaf:
                self._sel = i
                break

    def _build(
        self,
        current_id: str,
        current_messages: list[dict],
        sessions_dir: str | None,
    ) -> None:
        import pathlib

        all_info: dict[str, dict] = {}
        if sessions_dir:
            d = pathlib.Path(sessions_dir)
            if d.exists():
                for f in sorted(
                    d.rglob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    info = _read_session_info(str(f))
                    if info:
                        all_info[info["id"]] = info

        # Trace ancestors
        ancestors: list[dict] = []
        parent_id = all_info.get(current_id, {}).get("parent")
        seen: set[str] = set()
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            info = all_info.get(parent_id)
            if not info:
                break
            ancestors.insert(0, info)
            parent_id = info.get("parent")

        # Find direct children (forks)
        children = [i for i in all_info.values() if i.get("parent") == current_id]
        children.sort(key=lambda i: i.get("date", ""))

        nodes: list[TreeNode] = []

        # --- Ancestor session nodes ---
        for i, info in enumerate(ancestors):
            is_last = i == len(ancestors) - 1
            sid = info["id"][:8]
            date = info.get("date", "")
            msgs = info.get("msgs", 0)
            summary = f"{sid}  {date}  {msgs} msg(s)"
            nodes.append(TreeNode(
                node_type="session_header",
                display_text=theme.fg("dim", summary),
                raw_text=summary,
                session_id=info["id"],
                file_path=info.get("file"),
                is_current_session=False,
                is_on_active_path=False,
                indent=i,
                connector="└─ " if is_last else "├─ ",
            ))

        cur_indent = len(ancestors)

        # --- Current session messages ---
        total = len(current_messages)
        for j, msg in enumerate(current_messages):
            role = msg.get("role", "")
            preview = msg.get("preview", "")
            is_leaf = j == total - 1
            msg_idx = msg.get("message_index", j)
            colored, raw = _entry_line(role, preview)
            nodes.append(TreeNode(
                node_type=role if role in ("user", "assistant", "tool") else "session_header",
                display_text=colored,
                raw_text=raw,
                session_id=current_id,
                file_path=None,
                is_current_session=True,
                is_leaf=is_leaf,
                message_index=msg_idx,
                indent=cur_indent,
                connector="",
                is_on_active_path=True,
            ))

        if not current_messages:
            # Show placeholder when session has no messages yet
            nodes.append(TreeNode(
                node_type="session_header",
                display_text=theme.fg("dim", "(empty session)"),
                raw_text="(empty session)",
                session_id=current_id,
                is_current_session=True,
                is_leaf=True,
                indent=cur_indent,
                is_on_active_path=True,
            ))

        # --- Fork session nodes ---
        for i, info in enumerate(children):
            is_last = i == len(children) - 1
            sid = info["id"][:8]
            date = info.get("date", "")
            msgs = info.get("msgs", 0)
            summary = f"{sid}  {date}  {msgs} msg(s)  (fork)"
            nodes.append(TreeNode(
                node_type="session_header",
                display_text=theme.fg("dim", summary),
                raw_text=summary,
                session_id=info["id"],
                file_path=info.get("file"),
                is_current_session=False,
                is_on_active_path=False,
                indent=cur_indent + 1,
                connector="└─ " if is_last else "├─ ",
            ))

        self._nodes = nodes

    def _apply_filter(self) -> None:
        tokens = self._search_query.lower().split() if self._search_query else []
        result = []
        for node in self._nodes:
            if self._filter_mode == "user-only":
                if node.node_type not in ("user", "session_header"):
                    continue
            if tokens:
                if not all(t in node.raw_text.lower() for t in tokens):
                    continue
            result.append(node)
        self._filtered = result
        # Clamp selection
        if self._sel >= len(self._filtered):
            self._sel = max(0, len(self._filtered) - 1)

    def _filter_label(self) -> str:
        if self._filter_mode == "user-only":
            return " [user]"
        return ""

    def handle_input(self, data: str) -> None:
        from ..engine.keybindings import get_keybindings
        kb = get_keybindings()

        if kb.matches(data, "tui.select.up"):
            if self._filtered:
                self._sel = (self._sel - 1) % len(self._filtered)
        elif kb.matches(data, "tui.select.down"):
            if self._filtered:
                self._sel = (self._sel + 1) % len(self._filtered)
        elif kb.matches(data, "tui.select.pageUp"):
            self._sel = max(0, self._sel - 10)
        elif kb.matches(data, "tui.select.pageDown"):
            self._sel = min(len(self._filtered) - 1, self._sel + 10)
        elif kb.matches(data, "tui.select.confirm"):
            if self._filtered and self.on_select:
                self.on_select(self._filtered[self._sel])
        elif kb.matches(data, "tui.select.cancel"):
            if self._search_query:
                self._search_query = ""
                self._apply_filter()
            else:
                if self.on_cancel:
                    self.on_cancel()
        elif kb.matches(data, "tui.input.tab"):
            # Cycle filter mode
            idx = self._FILTER_MODES.index(self._filter_mode)
            self._filter_mode = self._FILTER_MODES[(idx + 1) % len(self._FILTER_MODES)]
            self._apply_filter()
        elif kb.matches(data, "tui.editor.deleteCharBackward"):
            if self._search_query:
                self._search_query = self._search_query[:-1]
                self._apply_filter()
        else:
            # Type to search (ignore control chars)
            has_ctrl = any(ord(c) < 32 or ord(c) == 0x7F for c in data)
            if not has_ctrl and data:
                self._search_query += data
                self._apply_filter()

    def render(self, width: int) -> list[str]:
        border_hex = theme._resolve("borderMuted")
        accent_hex = theme._resolve("accent")
        selected_bg = theme._resolve("selectedBg")
        lines: list[str] = []

        # Header
        lines.append(ansi.fg(border_hex, "─" * width))
        lines.append(ansi.truncate_to_width(
            ansi.bold(theme.fg("accent", "  Session Tree")), width
        ))
        lines.append(ansi.truncate_to_width(
            theme.fg("dim", "  ↑/↓: move  Tab: filter  Enter: select  Esc: close  Type: search"),
            width,
        ))
        # Search line
        if self._search_query:
            sq = (
                theme.fg("dim", "  search: ")
                + theme.fg("accent", self._search_query)
            )
        else:
            sq = theme.fg("dim", "  search:")
        lines.append(ansi.truncate_to_width(sq, width))
        lines.append(ansi.fg(border_hex, "─" * width))
        lines.append("")

        if not self._filtered:
            lines.append(theme.fg("dim", "  No entries found."))
            lines.append("")
            lines.append(ansi.fg(border_hex, "─" * width))
            count_line = theme.fg("dim", f"  (0/0){self._filter_label()}")
            lines.append(ansi.truncate_to_width(count_line, width))
            return lines

        # Visible window (centre selection, roughly half terminal)
        max_visible = max(5, self._terminal_height // 2)
        start = max(
            0,
            min(
                self._sel - max_visible // 2,
                len(self._filtered) - max_visible,
            ),
        )
        end = min(start + max_visible, len(self._filtered))

        for i in range(start, end):
            node = self._filtered[i]
            selected = i == self._sel

            cursor = ansi.fg(accent_hex, "› ") if selected else "  "
            indent_str = "  " * node.indent
            connector = node.connector

            # Active path marker
            path_marker = (
                ansi.fg(accent_hex, "• ") if node.is_on_active_path else "  "
            )

            # Build content
            if selected:
                # Bold the display text on selection
                content = ansi.bold(node.display_text)
            else:
                content = node.display_text

            # Leaf marker
            if node.is_leaf:
                content = content + theme.fg("dim", "  ←")

            row = cursor + theme.fg("dim", indent_str + connector) + path_marker + content

            if selected:
                row = ansi.bg(selected_bg, ansi.pad_to_width(row, width))

            lines.append(ansi.truncate_to_width(row, width))

        lines.append("")
        lines.append(ansi.fg(border_hex, "─" * width))
        count_str = f"  ({self._sel + 1}/{len(self._filtered)}){self._filter_label()}"
        lines.append(ansi.truncate_to_width(theme.fg("dim", count_str), width))

        return lines

    def invalidate(self) -> None:
        pass
