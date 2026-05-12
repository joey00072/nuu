"""ANSI escape code helpers, visible-width measurement, and text wrapping.

Ported from Pi's ref/pi/packages/tui/src/utils.ts.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable

import grapheme
from wcwidth import wcwidth

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDERLINE = "\x1b[4m"
BLINK = "\x1b[5m"
INVERSE = "\x1b[7m"
HIDDEN = "\x1b[8m"
STRIKETHROUGH = "\x1b[9m"
REVERSE = "\x1b[7m"
REVERSE_OFF = "\x1b[27m"

# Regex for ANSI escape sequences
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_APC_RE = re.compile(r"\x1b_[^\x07]*\x07")
_DCS_RE = re.compile(r"\x1b[PX^][^\x1b]*\x1b\\")
_SIMPLE_ESC_RE = re.compile(r"\x1b[^[]")

_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9;?]*[a-zA-Z]"  # CSI
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|_[^\x07]*\x07"  # APC
    r"|[PX^][^\x1b]*\x1b\\"  # DCS/SOS/PM
    r"|[^[]"  # 2-char escape sequences
    r")"
)

_ZERO_WIDTH_RE = re.compile(
    r"^[\u0000-\u001F\u007F-\u009F\u0300-\u036F\u0483-\u0489\u0591-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06DC\u06DF-\u06E4\u06E7\u06E8\u06EA-\u06ED\u0711\u0730-\u074A\u07A6-\u07B0\u07EB-\u07F3\u0816-\u0819\u081B-\u0823\u0825-\u0827\u0829-\u082D\u0859-\u085B\u08E4-\u08FE\u0900-\u0903\u093A-\u093C\u093E-\u094F\u0951-\u0957\u0962\u0963\u0981-\u0983\u09BC\u09BE-\u09C4\u09C7\u09C8\u09CB-\u09CD\u09D7\u09E2\u09E3\u0A01-\u0A03\u0A3C\u0A3E-\u0A42\u0A47\u0A48\u0A4B-\u0A4D\u0A51\u0A70\u0A71\u0A75\u0A81-\u0A83\u0ABC\u0ABE-\u0AC5\u0AC7-\u0AC9\u0ACB-\u0ACD\u0AE2\u0AE3\u0B01-\u0B03\u0B3C\u0B3E-\u0B44\u0B47\u0B48\u0B4B-\u0B4D\u0B56\u0B57\u0B62\u0B63\u0B82\u0BBE-\u0BC2\u0BC6-\u0BC8\u0BCA-\u0BCD\u0BD7\u0C00-\u0C03\u0C3E-\u0C44\u0C46-\u0C48\u0C4A-\u0C4D\u0C55\u0C56\u0C62\u0C63\u0C81-\u0C83\u0CBC\u0CBE-\u0CC4\u0CC6-\u0CC8\u0CCA-\u0CCD\u0CD5\u0CD6\u0CE2\u0CE3\u0D00-\u0D03\u0D3B\u0D3C\u0D3E-\u0D44\u0D46-\u0D48\u0D4A-\u0D4D\u0D57\u0D62\u0D63\u0D82\u0D83\u0DCA\u0DCF-\u0DD4\u0DD6\u0DD8-\u0DDF\u0DF2\u0DF3\u0E31\u0E34-\u0E3A\u0E47-\u0E4E\u0EB1\u0EB4-\u0EB9\u0EBB\u0EBC\u0EC8-\u0ECD\u0F18\u0F19\u0F35\u0F37\u0F39\u0F3E\u0F3F\u0F71-\u0F84\u0F86\u0F87\u0F8D-\u0F97\u0F99-\u0FBC\u0FC6\u102B-\u103E\u1056-\u1059\u105E-\u1060\u1062-\u1064\u1067-\u106D\u1071-\u1074\u1082-\u108D\u108F\u109A-\u109D\u135D-\u135F\u1712-\u1714\u1732-\u1734\u1752\u1753\u1772\u1773\u17B4-\u17D3\u17DD\u180B-\u180D\u1885\u1886\u18A9\u1920-\u192B\u1930-\u193B\u1A17-\u1A1B\u1A55-\u1A5E\u1A60-\u1A7C\u1A7F\u1AB0-\u1ABE\u1B00-\u1B04\u1B34-\u1B44\u1B6B-\u1B73\u1B80-\u1B82\u1BA1-\u1BAD\u1BE6-\u1BF3\u1C24-\u1C37\u1CD0-\u1CD2\u1CD4-\u1CE8\u1CED\u1CF2-\u1CF4\u1CF8\u1CF9\u1DC0-\u1DF9\u1DFB-\u1DFF\u20D0-\u20F0\u2CEF-\u2CF1\u2D7F\u2DE0-\u2DFF\u302A-\u302F\u3099\u309A\uA66F-\uA672\uA674-\uA67D\uA69E\uA69F\uA6F0\uA6F1\uA802\uA806\uA80B\uA823-\uA827\uA880\uA881\uA8B4-\uA8C5\uA8E0-\uA8F1\uA926-\uA92D\uA947-\uA953\uA980-\uA983\uA9B3-\uA9C0\uA9E5\uAA29-\uAA36\uAA43\uAA4C\uAA4D\uAA7B-\uAA7D\uAAB0\uAAB2-\uAAB4\uAAB7\uAAB8\uAABE\uAABF\uAAC1\uAAEB-\uAAEF\uAAF5\uAAF6\uABE3-\uABEA\uABEC\uABED\uFB1E\uFE00-\uFE0F\uFE20-\uFE2F]+$"
)

_WIDTH_CACHE: dict[str, int] = {}
_WIDTH_CACHE_SIZE = 512


# ---------------------------------------------------------------------------
# ANSI extraction
# ---------------------------------------------------------------------------

class _AnsiCodeResult:
    __slots__ = ("code", "length")

    def __init__(self, code: str, length: int) -> None:
        self.code = code
        self.length = length


def extract_ansi_code(text: str, pos: int) -> _AnsiCodeResult | None:
    """Extract an ANSI escape sequence starting at *pos*, or return None."""
    if pos >= len(text) or text[pos] != "\x1b":
        return None
    nxt = text[pos + 1 : pos + 2]
    if nxt == "[":
        # CSI
        j = pos + 2
        while j < len(text) and text[j] not in "mGKHJD":
            j += 1
        if j < len(text):
            return _AnsiCodeResult(text[pos : j + 1], j + 1 - pos)
        return None
    if nxt == "]":
        # OSC
        j = pos + 2
        while j < len(text):
            if text[j] == "\x07":
                return _AnsiCodeResult(text[pos : j + 1], j + 1 - pos)
            if text[j] == "\x1b" and j + 1 < len(text) and text[j + 1] == "\\":
                return _AnsiCodeResult(text[pos : j + 2], j + 2 - pos)
            j += 1
        return None
    if nxt == "_":
        # APC
        j = pos + 2
        while j < len(text):
            if text[j] == "\x07":
                return _AnsiCodeResult(text[pos : j + 1], j + 1 - pos)
            if text[j] == "\x1b" and j + 1 < len(text) and text[j + 1] == "\\":
                return _AnsiCodeResult(text[pos : j + 2], j + 2 - pos)
            j += 1
        return None
    return None


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Width
# ---------------------------------------------------------------------------


def _could_be_emoji(segment: str) -> bool:
    """Fast heuristic for possible emoji graphemes."""
    cp = ord(segment[0])
    return (
        (0x1F000 <= cp <= 0x1FBFF)
        or (0x2300 <= cp <= 0x23FF)
        or (0x2600 <= cp <= 0x27BF)
        or (0x2B50 <= cp <= 0x2B55)
        or ("\uFE0F" in segment)
        or len(segment) > 2
    )


def grapheme_width(segment: str) -> int:
    """Width of a single grapheme cluster in terminal columns."""
    if not segment:
        return 0

    # Zero-width clusters
    if _ZERO_WIDTH_RE.match(segment):
        return 0

    # Emoji check
    if _could_be_emoji(segment):
        # Use grapheme length as heuristic: most emoji are width 2
        # This is a simplification; RGI_Emoji regex would be more precise
        if grapheme.length(segment) == 1 and _could_be_emoji(segment):
            # Regional indicators are width 2
            cp = ord(segment[0])
            if 0x1F1E6 <= cp <= 0x1F1FF:
                return 2
            # Many emoji are width 2
            w = wcwidth(segment[0])
            return 2 if w == 2 else (w if w >= 0 else 1)

    # Use wcwidth on the first codepoint after stripping leading non-printing
    base = segment
    for ch in segment:
        cat = unicodedata.category(ch)
        if cat not in ("Mn", "Mc", "Me", "Cf", "Zl", "Zp", "Cc", "Cs"):
            base = ch
            break
    else:
        return 0

    w = wcwidth(base)
    if w is None or w < 0:
        w = 0

    # Trailing halfwidth/fullwidth forms and Thai/Lao AM vowels
    extra = 0
    for ch in segment[1:]:
        c = ord(ch)
        if 0xFF00 <= c <= 0xFFEF:
            extra += wcwidth(ch) or 0
        elif c in (0x0E33, 0x0EB3):
            extra += 1
    return w + extra


def visible_width(text: str) -> int:
    """Visible width of a string in terminal columns."""
    if not text:
        return 0

    cached = _WIDTH_CACHE.get(text)
    if cached is not None:
        return cached

    # Normalize tabs and strip ANSI
    clean = text.replace("\t", "   ")
    if "\x1b" in clean:
        stripped = ""
        i = 0
        while i < len(clean):
            ansi = extract_ansi_code(clean, i)
            if ansi:
                i += ansi.length
                continue
            stripped += clean[i]
            i += 1
        clean = stripped

    width = 0
    for g in grapheme.graphemes(clean):
        width += grapheme_width(g)

    if len(_WIDTH_CACHE) >= _WIDTH_CACHE_SIZE:
        _WIDTH_CACHE.pop(next(iter(_WIDTH_CACHE)))
    _WIDTH_CACHE[text] = width
    return width


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def fg(hex_color: str, text: str) -> str:
    if not hex_color:
        return text
    r, g, b = _hex_to_rgb(hex_color)
    return f"\x1b[38;2;{r};{g};{b}m{text}{RESET}"


def bg(hex_color: str, text: str) -> str:
    if not hex_color:
        return text
    r, g, b = _hex_to_rgb(hex_color)
    bg_code = f"\x1b[48;2;{r};{g};{b}m"
    # Re-apply bg after any inner RESET so inner fg codes don't kill the background.
    inner = text.replace(RESET, RESET + bg_code)
    return f"{bg_code}{inner}{RESET}"


def bold(text: str) -> str:
    return f"{BOLD}{text}{RESET}"


def italic(text: str) -> str:
    return f"{ITALIC}{text}{RESET}"


def dim(text: str) -> str:
    return f"{DIM}{text}{RESET}"


def underline(text: str) -> str:
    return f"{UNDERLINE}{text}{RESET}"


def strikethrough(text: str) -> str:
    return f"{STRIKETHROUGH}{text}{RESET}"


def reverse(text: str) -> str:
    return f"{REVERSE}{text}{RESET}"


# ---------------------------------------------------------------------------
# Truncation / padding
# ---------------------------------------------------------------------------


def _truncate_fragment(text: str, max_width: int) -> tuple[str, int]:
    """Truncate *text* to fit within *max_width* columns."""
    if max_width <= 0 or not text:
        return ("", 0)
    result = ""
    width = 0
    i = 0
    pending_ansi = ""
    while i < len(text):
        ansi = extract_ansi_code(text, i)
        if ansi:
            pending_ansi += ansi.code
            i += ansi.length
            continue
        if text[i] == "\t":
            tw = 3
            if width + tw > max_width:
                break
            if pending_ansi:
                result += pending_ansi
                pending_ansi = ""
            result += "\t"
            width += tw
            i += 1
            continue
        j = i
        while j < len(text) and text[j] != "\t" and not extract_ansi_code(text, j):
            j += 1
        for g in grapheme.graphemes(text[i:j]):
            gw = grapheme_width(g)
            if width + gw > max_width:
                return (result, width)
            if pending_ansi:
                result += pending_ansi
                pending_ansi = ""
            result += g
            width += gw
        i = j
    return (result, width)


def truncate_to_width(text: str, width: int, ellipsis: str = "...", pad: bool = False) -> str:
    """Truncate so visible width ≤ *width*, adding *ellipsis* if truncated."""
    if width <= 0:
        return ""
    vw = visible_width(text)
    if vw <= width:
        return text + (" " * (width - vw) if pad else "")
    ell_w = visible_width(ellipsis)
    if ell_w >= width:
        frag, fw = _truncate_fragment(ellipsis, width)
        return frag + (" " * (width - fw) if pad else "")
    target = width - ell_w
    frag, fw = _truncate_fragment(text, target)
    result = f"{frag}{RESET}{ellipsis}{RESET}"
    if pad:
        result += " " * (width - fw - ell_w)
    return result


def pad_to_width(text: str, width: int) -> str:
    """Right-pad with spaces so visible width equals *width*."""
    vw = visible_width(text)
    if vw >= width:
        return text
    return text + " " * (width - vw)


# ---------------------------------------------------------------------------
# Slicing / segmentation
# ---------------------------------------------------------------------------


def slice_with_width(line: str, start_col: int, length: int, strict: bool = False) -> tuple[str, int]:
    """Extract a slice of *line* by visible columns."""
    if length <= 0:
        return ("", 0)
    end_col = start_col + length
    result = ""
    result_width = 0
    current_col = 0
    i = 0
    pending_ansi = ""
    while i < len(line):
        ansi = extract_ansi_code(line, i)
        if ansi:
            if current_col >= start_col and current_col < end_col:
                result += ansi.code
            elif current_col < start_col:
                pending_ansi += ansi.code
            i += ansi.length
            continue
        j = i
        while j < len(line) and not extract_ansi_code(line, j):
            j += 1
        for g in grapheme.graphemes(line[i:j]):
            gw = grapheme_width(g)
            in_range = current_col >= start_col and current_col < end_col
            fits = not strict or current_col + gw <= end_col
            if in_range and fits:
                if pending_ansi:
                    result += pending_ansi
                    pending_ansi = ""
                result += g
                result_width += gw
            current_col += gw
            if current_col >= end_col:
                break
        i = j
        if current_col >= end_col:
            break
    return (result, result_width)


def slice_by_column(line: str, start_col: int, length: int, strict: bool = False) -> str:
    return slice_with_width(line, start_col, length, strict)[0]


class SegmentResult:
    __slots__ = ("before", "beforeWidth", "after", "afterWidth")

    def __init__(self, before: str, beforeWidth: int, after: str, afterWidth: int) -> None:
        self.before = before
        self.beforeWidth = beforeWidth
        self.after = after
        self.afterWidth = afterWidth


def extract_segments(
    line: str,
    before_end: int,
    after_start: int,
    after_len: int,
    strict_after: bool = False,
) -> SegmentResult:
    """Extract before/after segments for overlay compositing."""
    after_end = after_start + after_len
    before = ""
    before_width = 0
    after = ""
    after_width = 0
    current_col = 0
    i = 0
    pending_ansi_before = ""
    after_started = False

    tracker = AnsiCodeTracker()

    while i < len(line):
        ansi = extract_ansi_code(line, i)
        if ansi:
            code = ansi.code
            tracker.process(code)
            if current_col < before_end:
                pending_ansi_before += code
            elif current_col >= after_start and current_col < after_end and after_started:
                after += code
            i += ansi.length
            continue

        j = i
        while j < len(line) and not extract_ansi_code(line, j):
            j += 1

        for g in grapheme.graphemes(line[i:j]):
            gw = grapheme_width(g)
            if current_col < before_end:
                if pending_ansi_before:
                    before += pending_ansi_before
                    pending_ansi_before = ""
                before += g
                before_width += gw
            elif current_col >= after_start and current_col < after_end:
                fits = not strict_after or current_col + gw <= after_end
                if fits:
                    if not after_started:
                        after += tracker.get_active_codes()
                        after_started = True
                    after += g
                    after_width += gw
            current_col += gw
            if after_len <= 0:
                if current_col >= before_end:
                    break
            else:
                if current_col >= after_end:
                    break
        i = j
        if after_len <= 0:
            if current_col >= before_end:
                break
        else:
            if current_col >= after_end:
                break

    return SegmentResult(before, before_width, after, after_width)


# ---------------------------------------------------------------------------
# ANSI code tracker (preserves styles across line breaks)
# ---------------------------------------------------------------------------


class AnsiCodeTracker:
    """Track active ANSI SGR codes so styles survive wrapping."""

    def __init__(self) -> None:
        self.bold = False
        self.dim = False
        self.italic = False
        self.underline = False
        self.blink = False
        self.inverse = False
        self.hidden = False
        self.strikethrough = False
        self.fg_color: str | None = None
        self.bg_color: str | None = None
        self.active_hyperlink: dict[str, str] | None = None

    def clear(self) -> None:
        self.__init__()

    def reset(self) -> None:
        self.bold = False
        self.dim = False
        self.italic = False
        self.underline = False
        self.blink = False
        self.inverse = False
        self.hidden = False
        self.strikethrough = False
        self.fg_color = None
        self.bg_color = None
        # Note: SGR reset does NOT clear OSC 8 hyperlink state

    def process(self, ansi_code: str) -> None:
        if ansi_code.startswith("\x1b]8;"):
            self._process_hyperlink(ansi_code)
            return
        if not ansi_code.endswith("m"):
            return
        match = re.match(r"\x1b\[([\d;]*)m", ansi_code)
        if not match:
            return
        params = match.group(1)
        if params == "" or params == "0":
            self.reset()
            return
        parts = params.split(";")
        i = 0
        while i < len(parts):
            code = int(parts[i]) if parts[i].isdigit() else -1
            if code in (38, 48) and i + 2 < len(parts):
                if parts[i + 1] == "5" and parts[i + 2] is not None:
                    color_code = f"{parts[i]};{parts[i + 1]};{parts[i + 2]}"
                    if code == 38:
                        self.fg_color = color_code
                    else:
                        self.bg_color = color_code
                    i += 3
                    continue
                if parts[i + 1] == "2" and i + 4 < len(parts):
                    color_code = f"{parts[i]};{parts[i + 1]};{parts[i + 2]};{parts[i + 3]};{parts[i + 4]}"
                    if code == 38:
                        self.fg_color = color_code
                    else:
                        self.bg_color = color_code
                    i += 5
                    continue
            # Standard SGR codes
            if code == 0:
                self.reset()
            elif code == 1:
                self.bold = True
            elif code == 2:
                self.dim = True
            elif code == 3:
                self.italic = True
            elif code == 4:
                self.underline = True
            elif code == 5:
                self.blink = True
            elif code == 7:
                self.inverse = True
            elif code == 8:
                self.hidden = True
            elif code == 9:
                self.strikethrough = True
            elif code == 21:
                self.bold = False
            elif code == 22:
                self.bold = False
                self.dim = False
            elif code == 23:
                self.italic = False
            elif code == 24:
                self.underline = False
            elif code == 25:
                self.blink = False
            elif code == 27:
                self.inverse = False
            elif code == 28:
                self.hidden = False
            elif code == 29:
                self.strikethrough = False
            elif code == 39:
                self.fg_color = None
            elif code == 49:
                self.bg_color = None
            elif (30 <= code <= 37) or (90 <= code <= 97):
                self.fg_color = str(code)
            elif (40 <= code <= 47) or (100 <= code <= 107):
                self.bg_color = str(code)
            i += 1

    def _process_hyperlink(self, ansi_code: str) -> None:
        body = ansi_code[4:]
        if body.endswith("\x07"):
            terminator = "\x07"
            body = body[:-1]
        elif body.endswith("\x1b\\"):
            terminator = "\x1b\\"
            body = body[:-2]
        else:
            return
        sep = body.find(";")
        if sep == -1:
            return
        params = body[:sep]
        url = body[sep + 1 :]
        if not url:
            self.active_hyperlink = None
            return
        self.active_hyperlink = {"params": params, "url": url, "terminator": terminator}

    def get_active_codes(self) -> str:
        codes: list[str] = []
        if self.bold:
            codes.append("1")
        if self.dim:
            codes.append("2")
        if self.italic:
            codes.append("3")
        if self.underline:
            codes.append("4")
        if self.blink:
            codes.append("5")
        if self.inverse:
            codes.append("7")
        if self.hidden:
            codes.append("8")
        if self.strikethrough:
            codes.append("9")
        if self.fg_color:
            codes.append(self.fg_color)
        if self.bg_color:
            codes.append(self.bg_color)
        result = f"\x1b[{';'.join(codes)}m" if codes else ""
        if self.active_hyperlink:
            result += f"\x1b]8;{self.active_hyperlink['params']};{self.active_hyperlink['url']}{self.active_hyperlink['terminator']}"
        return result

    def has_active_codes(self) -> bool:
        return (
            self.bold
            or self.dim
            or self.italic
            or self.underline
            or self.blink
            or self.inverse
            or self.hidden
            or self.strikethrough
            or self.fg_color is not None
            or self.bg_color is not None
            or self.active_hyperlink is not None
        )

    def get_line_end_reset(self) -> str:
        result = ""
        if self.underline:
            result += "\x1b[24m"
        if self.active_hyperlink:
            term = self.active_hyperlink["terminator"]
            result += f"\x1b]8;;{term}"
        return result


# ---------------------------------------------------------------------------
# Wrapping
# ---------------------------------------------------------------------------


def _split_tokens_with_ansi(text: str) -> list[str]:
    tokens: list[str] = []
    current = ""
    pending_ansi = ""
    in_ws = False
    i = 0
    while i < len(text):
        ansi = extract_ansi_code(text, i)
        if ansi:
            pending_ansi += ansi.code
            i += ansi.length
            continue
        ch = text[i]
        is_ws = ch == " "
        if is_ws != in_ws and current:
            tokens.append(current)
            current = ""
        if pending_ansi:
            current += pending_ansi
            pending_ansi = ""
        in_ws = is_ws
        current += ch
        i += 1
    if pending_ansi:
        current += pending_ansi
    if current:
        tokens.append(current)
    return tokens


def _break_long_word(word: str, width: int, tracker: AnsiCodeTracker) -> list[str]:
    lines: list[str] = []
    current_line = tracker.get_active_codes()
    current_width = 0
    i = 0
    while i < len(word):
        ansi = extract_ansi_code(word, i)
        if ansi:
            code = ansi.code
            current_line += code
            tracker.process(code)
            i += ansi.length
            continue
        j = i
        while j < len(word) and not extract_ansi_code(word, j):
            j += 1
        for g in grapheme.graphemes(word[i:j]):
            gw = grapheme_width(g)
            if current_width + gw > width:
                reset = tracker.get_line_end_reset()
                if reset:
                    current_line += reset
                lines.append(current_line)
                current_line = tracker.get_active_codes()
                current_width = 0
            current_line += g
            current_width += gw
        i = j
    if current_line:
        lines.append(current_line)
    return lines if lines else [""]


def _wrap_single_line(line: str, width: int) -> list[str]:
    if not line:
        return [""]
    if visible_width(line) <= width:
        return [line]
    wrapped: list[str] = []
    tracker = AnsiCodeTracker()
    tokens = _split_tokens_with_ansi(line)
    current_line = ""
    current_visible = 0
    for token in tokens:
        token_vw = visible_width(token)
        is_ws = token.strip() == ""
        if token_vw > width and not is_ws:
            if current_line:
                reset = tracker.get_line_end_reset()
                if reset:
                    current_line += reset
                wrapped.append(current_line.rstrip())
                current_line = ""
                current_visible = 0
            broken = _break_long_word(token, width, tracker)
            wrapped.extend(broken[:-1])
            current_line = broken[-1]
            current_visible = visible_width(current_line)
            continue
        if current_visible + token_vw > width and current_visible > 0:
            reset = tracker.get_line_end_reset()
            if reset:
                current_line += reset
            wrapped.append(current_line.rstrip())
            if is_ws:
                current_line = tracker.get_active_codes()
                current_visible = 0
            else:
                current_line = tracker.get_active_codes() + token
                current_visible = token_vw
        else:
            current_line += token
            current_visible += token_vw
        # Update tracker with ANSI codes in token
        for ch in token:
            if ch == "\x1b":
                ansi = extract_ansi_code(token, token.index(ch))
                if ansi:
                    tracker.process(ansi.code)
    if current_line:
        wrapped.append(current_line.rstrip())
    return wrapped if wrapped else [""]


def wrap_text_with_ansi(text: str, width: int) -> list[str]:
    """Word-wrap text with ANSI codes preserved. Returns unpadded lines."""
    if not text:
        return [""]
    tracker = AnsiCodeTracker()
    result: list[str] = []
    for line in text.split("\n"):
        prefix = tracker.get_active_codes() if result else ""
        result.extend(_wrap_single_line(prefix + line, width))
        # Update tracker for next literal line
        i = 0
        while i < len(line):
            ansi = extract_ansi_code(line, i)
            if ansi:
                tracker.process(ansi.code)
                i += ansi.length
            else:
                i += 1
    return result if result else [""]


# ---------------------------------------------------------------------------
# Background / padding helpers
# ---------------------------------------------------------------------------


def apply_background_to_line(line: str, width: int, bg_fn: Callable[[str], str]) -> str:
    vw = visible_width(line)
    padding = " " * max(0, width - vw)
    return bg_fn(line + padding)


# ---------------------------------------------------------------------------
# Terminal output normalization
# ---------------------------------------------------------------------------

_THAI_LAO_AM_RE = re.compile("[\u0e33\u0eb3]")
_THAI_LAO_AM_MAP = {"\u0e33": "\u0e4d\u0e32", "\u0eb3": "\u0ecd\u0eb2"}


def normalize_terminal_output(text: str) -> str:
    if not _THAI_LAO_AM_RE.search(text):
        return text
    return _THAI_LAO_AM_RE.sub(lambda m: _THAI_LAO_AM_MAP[m.group(0)], text)
