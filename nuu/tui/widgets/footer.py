"""Footer component — Pi style: pwd/branch/session + token/cost/context stats."""

from __future__ import annotations

import os
from ..engine import ansi, theme


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{n // 1000}k"
    if n < 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n // 1_000_000}M"


class FooterComponent:
    """Two-line footer: cwd/branch/session and token/cost/context stats."""

    def __init__(self, model_label: str, cwd: str) -> None:
        self._model = model_label
        self._cwd = cwd
        self._busy = False

        # Accumulated totals across all assistant messages this session
        self._total_input: int = 0
        self._total_output: int = 0
        self._total_cache_read: int = 0
        self._total_cache_write: int = 0
        self._total_cost: float = 0.0

        # Last input token count as proxy for current context size
        self._last_input_tokens: int = 0
        self._context_window: int = 0

        # Supplementary display info
        self._git_branch: str | None = None
        self._session_name: str | None = None
        self._thinking_level: str | None = None  # None = model doesn't support reasoning

    def set_model(self, label: str) -> None:
        self._model = label

    def set_busy(self, busy: bool) -> None:
        self._busy = busy

    def set_git_branch(self, branch: str | None) -> None:
        self._git_branch = branch

    def set_session_name(self, name: str | None) -> None:
        self._session_name = name or None

    def set_context_window(self, size: int) -> None:
        self._context_window = size

    def set_thinking_level(self, level: str | None, model_supports_reasoning: bool) -> None:
        """level is current thinking level string; None if model doesn't support reasoning."""
        self._thinking_level = level if model_supports_reasoning else None

    def update_stats(self, usage: object) -> None:
        """Accumulate usage from one assistant message."""
        self._total_input += getattr(usage, "input", 0)
        self._total_output += getattr(usage, "output", 0)
        self._total_cache_read += getattr(usage, "cache_read", 0)
        self._total_cache_write += getattr(usage, "cache_write", 0)
        cost = getattr(usage, "cost", None)
        if cost is not None:
            self._total_cost += getattr(cost, "total", 0.0)
        self._last_input_tokens = getattr(usage, "input", 0)

    def render(self, width: int) -> list[str]:
        # ── Line 1: cwd (branch) • session ───────────────────────────────────
        home = os.path.expanduser("~")
        pwd = self._cwd
        if pwd.startswith(home):
            pwd = "~" + pwd[len(home):]
        if self._git_branch:
            pwd = f"{pwd} ({self._git_branch})"
        if self._session_name:
            pwd = f"{pwd} • {self._session_name}"
        pwd_line = ansi.truncate_to_width(
            theme.fg("dim", pwd), width, theme.fg("dim", "...")
        )

        # ── Line 2: stats left | model right ─────────────────────────────────
        parts: list[str] = []

        if self._total_input:
            parts.append(f"↑{_fmt_tokens(self._total_input)}")
        if self._total_output:
            parts.append(f"↓{_fmt_tokens(self._total_output)}")
        if self._total_cache_read:
            parts.append(f"R{_fmt_tokens(self._total_cache_read)}")
        if self._total_cache_write:
            parts.append(f"W{_fmt_tokens(self._total_cache_write)}")
        if self._total_cost > 0:
            parts.append(f"${self._total_cost:.3f}")

        # Context usage — colored by pressure
        if self._context_window > 0 and self._last_input_tokens > 0:
            pct = 100.0 * self._last_input_tokens / self._context_window
            pct_str = f"{pct:.1f}%/{_fmt_tokens(self._context_window)}"
            if pct > 90:
                ctx_part = ansi.fg(theme._resolve("error"), pct_str)
            elif pct > 70:
                ctx_part = ansi.fg(theme._resolve("warning"), pct_str)
            else:
                ctx_part = pct_str
            parts.append(ctx_part)
        elif self._context_window > 0:
            parts.append(f"?/{_fmt_tokens(self._context_window)}")

        stats_left = " ".join(parts)

        state_str = (
            theme.fg("accent", "● busy") if self._busy
            else theme.fg("dim", "● idle")
        )
        model_display = self._model
        if self._thinking_level is not None:
            suffix = "thinking off" if self._thinking_level == "off" else self._thinking_level
            model_display = f"{self._model} • {suffix}"
        model_str = theme.fg("dim", model_display)

        # Right side: state + model, left-pad to right-align
        right = state_str + "  " + model_str
        stats_w = ansi.visible_width(stats_left)
        right_w = ansi.visible_width(right)
        gap = max(1, width - stats_w - right_w)
        stats_line = ansi.fg(theme._resolve("dim"), stats_left) + " " * gap + right

        return [pwd_line, stats_line]

    def invalidate(self) -> None:
        pass
