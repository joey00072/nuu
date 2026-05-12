"""Markdown component using Rich for rendering."""

from __future__ import annotations
import io
from . import ansi


def render_markdown(text: str, width: int) -> list[str]:
    """Render markdown to a list of ANSI-colored lines at given width."""
    if not text.strip():
        return []
    try:
        from rich.console import Console
        from rich.theme import Theme
        from rich.markdown import Markdown as RichMarkdown
        from . import theme as tui_theme

        custom_theme = Theme({
            "markdown.code": tui_theme._resolve("mdCode") or "default",
            "markdown.code_block": "default",
            "markdown.item.bullet": tui_theme._resolve("mdListBullet") or "default",
        })

        buf = io.StringIO()
        console = Console(
            file=buf,
            width=width,
            force_terminal=True,
            highlight=False,
            markup=False,
            theme=custom_theme,
        )
        console.print(RichMarkdown(text), end="")
        output = buf.getvalue()
        # Split into lines, drop trailing empty line from Rich
        lines = output.split("\n")
        while lines and not ansi.strip_ansi(lines[-1]).strip():
            lines.pop()
        return lines if lines else [""]
    except Exception:
        # Fallback: plain text
        return text.splitlines() or [""]


class MarkdownComponent:
    """Renders markdown text as a component."""

    def __init__(self, text: str = "", padding_x: int = 1) -> None:
        self._text = text
        self._px = padding_x
        self._cache: tuple[int, list[str]] | None = None

    def set_text(self, text: str) -> None:
        if self._text != text:
            self._text = text
            self._cache = None

    def render(self, width: int) -> list[str]:
        if self._cache and self._cache[0] == width:
            return self._cache[1]

        inner_w = max(1, width - self._px * 2)
        md_lines = render_markdown(self._text, inner_w)
        result = [" " * self._px + line for line in md_lines]
        self._cache = (width, result)
        return result

    def invalidate(self) -> None:
        self._cache = None
