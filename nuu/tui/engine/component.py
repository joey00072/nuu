"""Component protocol and built-in layout primitives."""

from __future__ import annotations
from typing import Callable, Protocol, runtime_checkable
from . import ansi


@runtime_checkable
class Component(Protocol):
    def render(self, width: int) -> list[str]: ...
    def invalidate(self) -> None: ...


@runtime_checkable
class Focusable(Protocol):
    focused: bool


class Container:
    """Renders children top-to-bottom."""

    def __init__(self) -> None:
        self.children: list[Component] = []

    def add(self, child: Component) -> None:
        self.children.append(child)

    def remove(self, child: Component) -> None:
        try:
            self.children.remove(child)
        except ValueError:
            pass

    def clear(self) -> None:
        self.children.clear()

    def render(self, width: int) -> list[str]:
        lines: list[str] = []
        for child in self.children:
            lines.extend(child.render(width))
        return lines

    def invalidate(self) -> None:
        for child in self.children:
            child.invalidate()


class Spacer:
    """Fixed number of blank lines."""

    def __init__(self, lines: int = 1) -> None:
        self._lines = lines

    def render(self, width: int) -> list[str]:
        return [""] * self._lines

    def invalidate(self) -> None:
        pass


class Text:
    """Multi-line text with optional padding and background."""

    def __init__(
        self,
        text: str = "",
        padding_x: int = 0,
        padding_y: int = 0,
        bg_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._text = text
        self._px = padding_x
        self._py = padding_y
        self._bg_fn = bg_fn
        self._cache: tuple[int, list[str]] | None = None

    def set_text(self, text: str) -> None:
        if self._text != text:
            self._text = text
            self._cache = None

    def set_bg_fn(self, fn: Callable[[str], str] | None) -> None:
        self._bg_fn = fn
        self._cache = None

    def render(self, width: int) -> list[str]:
        if self._cache and self._cache[0] == width:
            return self._cache[1]

        inner_w = max(1, width - self._px * 2)
        raw_lines = self._wrap(self._text, inner_w)
        result: list[str] = []

        pad_h = " " * width
        for _ in range(self._py):
            result.append(self._apply_bg(pad_h, width))

        for line in raw_lines:
            padded = " " * self._px + ansi.pad_to_width(line, inner_w) + " " * self._px
            result.append(self._apply_bg(padded, width))

        for _ in range(self._py):
            result.append(self._apply_bg(pad_h, width))

        self._cache = (width, result)
        return result

    def _apply_bg(self, line: str, width: int) -> str:
        if self._bg_fn:
            return self._bg_fn(ansi.pad_to_width(line, width))
        return line

    def invalidate(self) -> None:
        self._cache = None

    @staticmethod
    def _wrap(text: str, width: int) -> list[str]:
        """Word-wrap plain text to width."""
        if not text:
            return [""]
        lines: list[str] = []
        for paragraph in text.splitlines():
            if not paragraph:
                lines.append("")
                continue
            current = ""
            current_w = 0
            for word in paragraph.split(" "):
                word_w = ansi.visible_width(word)
                if not current:
                    current = word
                    current_w = word_w
                elif current_w + 1 + word_w <= width:
                    current += " " + word
                    current_w += 1 + word_w
                else:
                    lines.append(current)
                    current = word
                    current_w = word_w
            if current or not lines:
                lines.append(current)
        return lines if lines else [""]


class Box:
    """Container with padding and background."""

    def __init__(
        self,
        padding_x: int = 1,
        padding_y: int = 1,
        bg_fn: Callable[[str], str] | None = None,
    ) -> None:
        self._px = padding_x
        self._py = padding_y
        self._bg_fn = bg_fn
        self.children: list[Component] = []

    def add(self, child: Component) -> None:
        self.children.append(child)

    def clear(self) -> None:
        self.children.clear()

    def set_bg_fn(self, fn: Callable[[str], str] | None) -> None:
        self._bg_fn = fn

    def render(self, width: int) -> list[str]:
        inner_w = max(1, width - self._px * 2)
        inner_lines: list[str] = []
        for child in self.children:
            inner_lines.extend(child.render(inner_w))

        result: list[str] = []
        blank = " " * width
        for _ in range(self._py):
            result.append(self._bg(blank, width))
        for line in inner_lines:
            padded = " " * self._px + ansi.pad_to_width(line, inner_w) + " " * self._px
            result.append(self._bg(padded, width))
        for _ in range(self._py):
            result.append(self._bg(blank, width))
        return result

    def _bg(self, line: str, width: int) -> str:
        if self._bg_fn:
            return self._bg_fn(ansi.pad_to_width(line, width))
        return line

    def invalidate(self) -> None:
        for child in self.children:
            child.invalidate()


class DynamicBorder:
    """Full-width horizontal rule."""

    def __init__(self, color_fn: Callable[[str], str] | None = None) -> None:
        self._color_fn = color_fn

    def render(self, width: int) -> list[str]:
        line = "─" * max(1, width)
        return [self._color_fn(line) if self._color_fn else line]

    def invalidate(self) -> None:
        pass
