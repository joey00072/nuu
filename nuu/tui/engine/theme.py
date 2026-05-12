"""Pi dark theme color palette and semantic color API."""

from __future__ import annotations
from . import ansi as _a

# Raw palette
_PALETTE = {
    "cyan":         "#00d7ff",
    "blue":         "#5f87ff",
    "green":        "#b5bd68",
    "red":          "#cc6666",
    "yellow":       "#ffff00",
    "gray":         "#808080",
    "dimGray":      "#666666",
    "darkGray":     "#505050",
    "accent":       "#8abeb7",
    "selectedBg":   "#3a3a4a",
    "userMsgBg":    "#343541",
    "toolPendingBg":"#282832",
    "toolSuccessBg":"#283228",
    "toolErrorBg":  "#3c2828",
    "customMsgBg":  "#2d2838",
}

# Semantic colors (name -> palette key or hex)
_COLORS: dict[str, str] = {
    "accent":            "accent",
    "border":            "blue",
    "borderAccent":      "cyan",
    "borderMuted":       "darkGray",
    "success":           "green",
    "error":             "red",
    "warning":           "yellow",
    "muted":             "gray",
    "dim":               "dimGray",
    "text":              "",
    "thinkingText":      "gray",

    "userMessageBg":     "userMsgBg",
    "userMessageText":   "",
    "toolPendingBg":     "toolPendingBg",
    "toolSuccessBg":     "toolSuccessBg",
    "toolErrorBg":       "toolErrorBg",

    "mdHeading":         "#f0c674",
    "mdCode":            "accent",
    "mdCodeBlock":       "green",
    "mdCodeBlockBorder": "gray",
    "mdLink":            "#81a2be",
    "mdLinkUrl":         "dimGray",
    "mdQuote":           "gray",
    "mdListBullet":      "accent",

    "toolDiffAdded":     "green",
    "toolDiffRemoved":   "red",
    "toolDiffContext":   "gray",
}


def _resolve(name: str) -> str:
    """Resolve a semantic color name to a hex string (or empty string)."""
    raw = _COLORS.get(name, name)
    return _PALETTE.get(raw, raw)  # raw may already be hex or ""


def fg(color_name: str, text: str) -> str:
    hex_color = _resolve(color_name)
    return _a.fg(hex_color, text) if hex_color else text


def bg(color_name: str, text: str) -> str:
    hex_color = _resolve(color_name)
    return _a.bg(hex_color, text) if hex_color else text


def bold(text: str) -> str:
    return _a.bold(text)


def italic(text: str) -> str:
    return _a.italic(text)


def dim(text: str) -> str:
    return _a.dim(text)
