"""Keyboard input handling for terminal applications.

Supports legacy terminal sequences and Kitty keyboard protocol.
Ported from Pi's ref/pi/packages/tui/src/keys.ts.
"""

from __future__ import annotations

import os
import re

# ---------------------------------------------------------------------------
# Global Kitty protocol state
# ---------------------------------------------------------------------------

_kitty_protocol_active = False


def set_kitty_protocol_active(active: bool) -> None:
    global _kitty_protocol_active
    _kitty_protocol_active = active


def is_kitty_protocol_active() -> bool:
    return _kitty_protocol_active


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

KeyId = str


class Key:
    """Helper for creating typed key identifiers."""

    escape = "escape"
    esc = "esc"
    enter = "enter"
    return_ = "return"
    tab = "tab"
    space = "space"
    backspace = "backspace"
    delete = "delete"
    insert = "insert"
    clear = "clear"
    home = "home"
    end = "end"
    pageUp = "pageUp"
    pageDown = "pageDown"
    up = "up"
    down = "down"
    left = "left"
    right = "right"
    f1 = "f1"
    f2 = "f2"
    f3 = "f3"
    f4 = "f4"
    f5 = "f5"
    f6 = "f6"
    f7 = "f7"
    f8 = "f8"
    f9 = "f9"
    f10 = "f10"
    f11 = "f11"
    f12 = "f12"

    @staticmethod
    def ctrl(key: str) -> str:
        return f"ctrl+{key}"

    @staticmethod
    def shift(key: str) -> str:
        return f"shift+{key}"

    @staticmethod
    def alt(key: str) -> str:
        return f"alt+{key}"

    @staticmethod
    def super_(key: str) -> str:
        return f"super+{key}"

    @staticmethod
    def ctrl_shift(key: str) -> str:
        return f"ctrl+shift+{key}"

    @staticmethod
    def ctrl_alt(key: str) -> str:
        return f"ctrl+alt+{key}"

    @staticmethod
    def shift_alt(key: str) -> str:
        return f"shift+alt+{key}"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYMBOL_KEYS = set(
    "`-=[]\\;',./!@#$%^&*()_+|~{}:<>?"
)

MODIFIERS = {"shift": 1, "alt": 2, "ctrl": 4, "super": 8}
LOCK_MASK = 64 + 128

CODEPOINTS = {"escape": 27, "tab": 9, "enter": 13, "space": 32, "backspace": 127, "kpEnter": 57414}

ARROW_CODEPOINTS = {"up": -1, "down": -2, "right": -3, "left": -4}

FUNCTIONAL_CODEPOINTS = {
    "delete": -10,
    "insert": -11,
    "pageUp": -12,
    "pageDown": -13,
    "home": -14,
    "end": -15,
}

KITTY_FUNCTIONAL_EQUIVALENTS = {
    57399: 48,
    57400: 49,
    57401: 50,
    57402: 51,
    57403: 52,
    57404: 53,
    57405: 54,
    57406: 55,
    57407: 56,
    57408: 57,
    57409: 46,
    57410: 47,
    57411: 42,
    57412: 45,
    57413: 43,
    57415: 61,
    57416: 44,
    57417: ARROW_CODEPOINTS["left"],
    57418: ARROW_CODEPOINTS["right"],
    57419: ARROW_CODEPOINTS["up"],
    57420: ARROW_CODEPOINTS["down"],
    57421: FUNCTIONAL_CODEPOINTS["pageUp"],
    57422: FUNCTIONAL_CODEPOINTS["pageDown"],
    57423: FUNCTIONAL_CODEPOINTS["home"],
    57424: FUNCTIONAL_CODEPOINTS["end"],
    57425: FUNCTIONAL_CODEPOINTS["insert"],
    57426: FUNCTIONAL_CODEPOINTS["delete"],
}

LEGACY_KEY_SEQUENCES = {
    "up": ["\x1b[A", "\x1bOA"],
    "down": ["\x1b[B", "\x1bOB"],
    "right": ["\x1b[C", "\x1bOC"],
    "left": ["\x1b[D", "\x1bOD"],
    "home": ["\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~"],
    "end": ["\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~"],
    "insert": ["\x1b[2~"],
    "delete": ["\x1b[3~"],
    "pageUp": ["\x1b[5~", "\x1b[[5~"],
    "pageDown": ["\x1b[6~", "\x1b[[6~"],
    "clear": ["\x1b[E", "\x1bOE"],
    "f1": ["\x1bOP", "\x1b[11~", "\x1b[[A"],
    "f2": ["\x1bOQ", "\x1b[12~", "\x1b[[B"],
    "f3": ["\x1bOR", "\x1b[13~", "\x1b[[C"],
    "f4": ["\x1bOS", "\x1b[14~", "\x1b[[D"],
    "f5": ["\x1b[15~", "\x1b[[E"],
    "f6": ["\x1b[17~"],
    "f7": ["\x1b[18~"],
    "f8": ["\x1b[19~"],
    "f9": ["\x1b[20~"],
    "f10": ["\x1b[21~"],
    "f11": ["\x1b[23~"],
    "f12": ["\x1b[24~"],
}

LEGACY_SHIFT_SEQUENCES = {
    "up": ["\x1b[a"],
    "down": ["\x1b[b"],
    "right": ["\x1b[c"],
    "left": ["\x1b[d"],
    "clear": ["\x1b[e"],
    "insert": ["\x1b[2$"],
    "delete": ["\x1b[3$"],
    "pageUp": ["\x1b[5$"],
    "pageDown": ["\x1b[6$"],
    "home": ["\x1b[7$"],
    "end": ["\x1b[8$"],
}

LEGACY_CTRL_SEQUENCES = {
    "up": ["\x1bOa"],
    "down": ["\x1bOb"],
    "right": ["\x1bOc"],
    "left": ["\x1bOd"],
    "clear": ["\x1bOe"],
    "insert": ["\x1b[2^"],
    "delete": ["\x1b[3^"],
    "pageUp": ["\x1b[5^"],
    "pageDown": ["\x1b[6^"],
    "home": ["\x1b[7^"],
    "end": ["\x1b[8^"],
}

LEGACY_SEQUENCE_KEY_IDS = {
    "\x1bOA": "up",
    "\x1bOB": "down",
    "\x1bOC": "right",
    "\x1bOD": "left",
    "\x1bOH": "home",
    "\x1bOF": "end",
    "\x1b[E": "clear",
    "\x1bOE": "clear",
    "\x1bOe": "ctrl+clear",
    "\x1b[e": "shift+clear",
    "\x1b[2~": "insert",
    "\x1b[2$": "shift+insert",
    "\x1b[2^": "ctrl+insert",
    "\x1b[3$": "shift+delete",
    "\x1b[3^": "ctrl+delete",
    "\x1b[[5~": "pageUp",
    "\x1b[[6~": "pageDown",
    "\x1b[a": "shift+up",
    "\x1b[b": "shift+down",
    "\x1b[c": "shift+right",
    "\x1b[d": "shift+left",
    "\x1bOa": "ctrl+up",
    "\x1bOb": "ctrl+down",
    "\x1bOc": "ctrl+right",
    "\x1bOd": "ctrl+left",
    "\x1b[5$": "shift+pageUp",
    "\x1b[6$": "shift+pageDown",
    "\x1b[7$": "shift+home",
    "\x1b[8$": "shift+end",
    "\x1b[5^": "ctrl+pageUp",
    "\x1b[6^": "ctrl+pageDown",
    "\x1b[7^": "ctrl+home",
    "\x1b[8^": "ctrl+end",
    "\x1bOP": "f1",
    "\x1bOQ": "f2",
    "\x1bOR": "f3",
    "\x1bOS": "f4",
    "\x1b[11~": "f1",
    "\x1b[12~": "f2",
    "\x1b[13~": "f3",
    "\x1b[14~": "f4",
    "\x1b[[A": "f1",
    "\x1b[[B": "f2",
    "\x1b[[C": "f3",
    "\x1b[[D": "f4",
    "\x1b[[E": "f5",
    "\x1b[15~": "f5",
    "\x1b[17~": "f6",
    "\x1b[18~": "f7",
    "\x1b[19~": "f8",
    "\x1b[20~": "f9",
    "\x1b[21~": "f10",
    "\x1b[23~": "f11",
    "\x1b[24~": "f12",
    "\x1bb": "alt+left",
    "\x1bf": "alt+right",
    "\x1bp": "alt+up",
    "\x1bn": "alt+down",
}

# ---------------------------------------------------------------------------
# Kitty parsing
# ---------------------------------------------------------------------------


class ParsedKittySequence:
    def __init__(
        self,
        codepoint: int,
        modifier: int,
        event_type: str,
        shifted_key: int | None = None,
        base_layout_key: int | None = None,
    ) -> None:
        self.codepoint = codepoint
        self.modifier = modifier
        self.event_type = event_type
        self.shifted_key = shifted_key
        self.base_layout_key = base_layout_key


def _parse_event_type(event_type_str: str | None) -> str:
    if not event_type_str:
        return "press"
    et = int(event_type_str)
    if et == 2:
        return "repeat"
    if et == 3:
        return "release"
    return "press"


def parse_kitty_sequence(data: str) -> ParsedKittySequence | None:
    # CSI u: \x1b[<codepoint>u  or  \x1b[<codepoint>;<mod>u  or  \x1b[<codepoint>;<mod>:<event>u
    # With shifted/base: \x1b[<codepoint>:<shifted>;<mod>u  or  \x1b[<codepoint>:<shifted>:<base>;<mod>u
    m = re.match(r"^\x1b\[(\d+)(?::(\d*))?(?::(\d+))?(?:;(\d+))?(?::(\d+))?u$", data)
    if m:
        codepoint = int(m.group(1))
        shifted_key = int(m.group(2)) if m.group(2) else None
        base_layout_key = int(m.group(3)) if m.group(3) else None
        mod_value = int(m.group(4)) if m.group(4) else 1
        event_type = _parse_event_type(m.group(5))
        return ParsedKittySequence(codepoint, mod_value - 1, event_type, shifted_key, base_layout_key)

    # Arrows: \x1b[1;<mod>A/B/C/D
    m = re.match(r"^\x1b\[1;(\d+)(?::(\d+))?([ABCD])$", data)
    if m:
        mod_value = int(m.group(1))
        event_type = _parse_event_type(m.group(2))
        arrow_codes = {"A": -1, "B": -2, "C": -3, "D": -4}
        return ParsedKittySequence(arrow_codes[m.group(3)], mod_value - 1, event_type)

    # Functional keys: \x1b[<num>~
    m = re.match(r"^\x1b\[(\d+)(?:;(\d+))?(?::(\d+))?~$", data)
    if m:
        key_num = int(m.group(1))
        mod_value = int(m.group(2)) if m.group(2) else 1
        event_type = _parse_event_type(m.group(3))
        func_codes = {2: FUNCTIONAL_CODEPOINTS["insert"], 3: FUNCTIONAL_CODEPOINTS["delete"], 5: FUNCTIONAL_CODEPOINTS["pageUp"], 6: FUNCTIONAL_CODEPOINTS["pageDown"], 7: FUNCTIONAL_CODEPOINTS["home"], 8: FUNCTIONAL_CODEPOINTS["end"]}
        cp = func_codes.get(key_num)
        if cp is not None:
            return ParsedKittySequence(cp, mod_value - 1, event_type)

    # Home/End with modifier: \x1b[1;<mod>H/F
    m = re.match(r"^\x1b\[1;(\d+)(?::(\d+))?([HF])$", data)
    if m:
        mod_value = int(m.group(1))
        event_type = _parse_event_type(m.group(2))
        cp = FUNCTIONAL_CODEPOINTS["home"] if m.group(3) == "H" else FUNCTIONAL_CODEPOINTS["end"]
        return ParsedKittySequence(cp, mod_value - 1, event_type)

    return None


def _normalize_kitty_functional_codepoint(codepoint: int) -> int:
    return KITTY_FUNCTIONAL_EQUIVALENTS.get(codepoint, codepoint)


def _normalize_shifted_letter(codepoint: int, modifier: int) -> int:
    effective = modifier & ~LOCK_MASK
    if (effective & MODIFIERS["shift"]) and 65 <= codepoint <= 90:
        return codepoint + 32
    return codepoint


def _matches_kitty_sequence(data: str, expected_codepoint: int, expected_modifier: int) -> bool:
    parsed = parse_kitty_sequence(data)
    if not parsed:
        return False
    actual_mod = parsed.modifier & ~LOCK_MASK
    expected_mod = expected_modifier & ~LOCK_MASK
    if actual_mod != expected_mod:
        return False
    normalized_cp = _normalize_shifted_letter(_normalize_kitty_functional_codepoint(parsed.codepoint), parsed.modifier)
    normalized_expected = _normalize_shifted_letter(_normalize_kitty_functional_codepoint(expected_codepoint), expected_modifier)
    if normalized_cp == normalized_expected:
        return True
    # Fallback to base layout key for non-Latin layouts
    if parsed.base_layout_key is not None and parsed.base_layout_key == expected_codepoint:
        cp = normalized_cp
        is_latin = 97 <= cp <= 122
        is_symbol = chr(cp) in SYMBOL_KEYS if 32 <= cp <= 126 else False
        if not is_latin and not is_symbol:
            return True
    return False


# ---------------------------------------------------------------------------
# modifyOtherKeys parsing
# ---------------------------------------------------------------------------


class ParsedModifyOtherKeysSequence:
    def __init__(self, codepoint: int, modifier: int) -> None:
        self.codepoint = codepoint
        self.modifier = modifier


def parse_modify_other_keys_sequence(data: str) -> ParsedModifyOtherKeysSequence | None:
    m = re.match(r"^\x1b\[27;(\d+);(\d+)~$", data)
    if not m:
        return None
    mod_value = int(m.group(1))
    codepoint = int(m.group(2))
    return ParsedModifyOtherKeysSequence(codepoint, mod_value - 1)


def _matches_modify_other_keys(data: str, expected_keycode: int, expected_modifier: int) -> bool:
    parsed = parse_modify_other_keys_sequence(data)
    if not parsed:
        return False
    return parsed.codepoint == expected_keycode and parsed.modifier == expected_modifier


def _matches_printable_modify_other_keys(data: str, expected_keycode: int, expected_modifier: int) -> bool:
    if expected_modifier == 0:
        return False
    parsed = parse_modify_other_keys_sequence(data)
    if not parsed or parsed.modifier != expected_modifier:
        return False
    return _normalize_shifted_letter(parsed.codepoint, parsed.modifier) == _normalize_shifted_letter(expected_keycode, expected_modifier)


# ---------------------------------------------------------------------------
# Legacy helpers
# ---------------------------------------------------------------------------


def _matches_legacy_sequence(data: str, sequences: list[str]) -> bool:
    return data in sequences


def _matches_legacy_modifier_sequence(data: str, key: str, modifier: int) -> bool:
    if modifier == MODIFIERS["shift"]:
        return _matches_legacy_sequence(data, LEGACY_SHIFT_SEQUENCES.get(key, []))
    if modifier == MODIFIERS["ctrl"]:
        return _matches_legacy_sequence(data, LEGACY_CTRL_SEQUENCES.get(key, []))
    return False


def _is_windows_terminal_session() -> bool:
    return bool(os.environ.get("WT_SESSION") and not os.environ.get("SSH_CONNECTION") and not os.environ.get("SSH_CLIENT") and not os.environ.get("SSH_TTY"))


def _matches_raw_backspace(data: str, expected_modifier: int) -> bool:
    if data == "\x7f":
        return expected_modifier == 0
    if data != "\x08":
        return False
    return _is_windows_terminal_session() if expected_modifier == MODIFIERS["ctrl"] else expected_modifier == 0


# ---------------------------------------------------------------------------
# Generic matching
# ---------------------------------------------------------------------------


def _raw_ctrl_char(key: str) -> str | None:
    char = key.lower()
    code = ord(char)
    if (97 <= code <= 122) or char in "[\\]_":
        return chr(code & 0x1F)
    if char == "-":
        return chr(31)
    return None


def _is_digit_key(key: str) -> bool:
    return len(key) == 1 and "0" <= key <= "9"


def _parse_key_id(key_id: str) -> dict[str, str | bool] | None:
    parts = key_id.lower().split("+")
    key = parts[-1]
    if not key:
        return None
    return {
        "key": key,
        "ctrl": "ctrl" in parts,
        "shift": "shift" in parts,
        "alt": "alt" in parts,
        "super": "super" in parts,
    }


def matches_key(data: str, key_id: KeyId) -> bool:
    parsed = _parse_key_id(key_id)
    if not parsed:
        return False

    key = parsed["key"]
    assert isinstance(key, str)
    ctrl = parsed["ctrl"]
    shift = parsed["shift"]
    alt = parsed["alt"]
    super_ = parsed["super"]
    modifier = 0
    if shift:
        modifier |= MODIFIERS["shift"]
    if alt:
        modifier |= MODIFIERS["alt"]
    if ctrl:
        modifier |= MODIFIERS["ctrl"]
    if super_:
        modifier |= MODIFIERS["super"]

    if key in ("escape", "esc"):
        if modifier != 0:
            return False
        return data == "\x1b" or _matches_kitty_sequence(data, CODEPOINTS["escape"], 0) or _matches_modify_other_keys(data, CODEPOINTS["escape"], 0)

    if key == "space":
        if not _kitty_protocol_active:
            if modifier == MODIFIERS["ctrl"] and data == "\x00":
                return True
            if modifier == MODIFIERS["alt"] and data == "\x1b ":
                return True
        if modifier == 0:
            return data == " " or _matches_kitty_sequence(data, CODEPOINTS["space"], 0) or _matches_modify_other_keys(data, CODEPOINTS["space"], 0)
        return _matches_kitty_sequence(data, CODEPOINTS["space"], modifier) or _matches_modify_other_keys(data, CODEPOINTS["space"], modifier)

    if key == "tab":
        if modifier == MODIFIERS["shift"]:
            return data == "\x1b[Z" or _matches_kitty_sequence(data, CODEPOINTS["tab"], MODIFIERS["shift"]) or _matches_modify_other_keys(data, CODEPOINTS["tab"], MODIFIERS["shift"])
        if modifier == 0:
            return data == "\t" or _matches_kitty_sequence(data, CODEPOINTS["tab"], 0)
        return _matches_kitty_sequence(data, CODEPOINTS["tab"], modifier) or _matches_modify_other_keys(data, CODEPOINTS["tab"], modifier)

    if key in ("enter", "return"):
        if modifier == MODIFIERS["shift"]:
            if _matches_kitty_sequence(data, CODEPOINTS["enter"], MODIFIERS["shift"]) or _matches_kitty_sequence(data, CODEPOINTS["kpEnter"], MODIFIERS["shift"]):
                return True
            if _matches_modify_other_keys(data, CODEPOINTS["enter"], MODIFIERS["shift"]):
                return True
            if _kitty_protocol_active:
                return data in ("\x1b\r", "\n")
            return False
        if modifier == MODIFIERS["alt"]:
            if _matches_kitty_sequence(data, CODEPOINTS["enter"], MODIFIERS["alt"]) or _matches_kitty_sequence(data, CODEPOINTS["kpEnter"], MODIFIERS["alt"]):
                return True
            if _matches_modify_other_keys(data, CODEPOINTS["enter"], MODIFIERS["alt"]):
                return True
            if not _kitty_protocol_active:
                return data == "\x1b\r"
            return False
        if modifier == 0:
            return data == "\r" or (not _kitty_protocol_active and data == "\n") or data == "\x1bOM" or _matches_kitty_sequence(data, CODEPOINTS["enter"], 0) or _matches_kitty_sequence(data, CODEPOINTS["kpEnter"], 0)
        return _matches_kitty_sequence(data, CODEPOINTS["enter"], modifier) or _matches_kitty_sequence(data, CODEPOINTS["kpEnter"], modifier) or _matches_modify_other_keys(data, CODEPOINTS["enter"], modifier)

    if key == "backspace":
        if modifier == MODIFIERS["alt"]:
            return data in ("\x1b\x7f", "\x1b\x08") or _matches_kitty_sequence(data, CODEPOINTS["backspace"], MODIFIERS["alt"]) or _matches_modify_other_keys(data, CODEPOINTS["backspace"], MODIFIERS["alt"])
        if modifier == MODIFIERS["ctrl"]:
            if _matches_raw_backspace(data, MODIFIERS["ctrl"]):
                return True
            return _matches_kitty_sequence(data, CODEPOINTS["backspace"], MODIFIERS["ctrl"]) or _matches_modify_other_keys(data, CODEPOINTS["backspace"], MODIFIERS["ctrl"])
        if modifier == 0:
            return _matches_raw_backspace(data, 0) or _matches_kitty_sequence(data, CODEPOINTS["backspace"], 0) or _matches_modify_other_keys(data, CODEPOINTS["backspace"], 0)
        return _matches_kitty_sequence(data, CODEPOINTS["backspace"], modifier) or _matches_modify_other_keys(data, CODEPOINTS["backspace"], modifier)

    if key == "insert":
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["insert"]) or _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["insert"], 0)
        if _matches_legacy_modifier_sequence(data, "insert", modifier):
            return True
        return _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["insert"], modifier)

    if key == "delete":
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["delete"]) or _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["delete"], 0)
        if _matches_legacy_modifier_sequence(data, "delete", modifier):
            return True
        return _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["delete"], modifier)

    if key == "clear":
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["clear"])
        return _matches_legacy_modifier_sequence(data, "clear", modifier)

    if key == "home":
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["home"]) or _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["home"], 0)
        if _matches_legacy_modifier_sequence(data, "home", modifier):
            return True
        return _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["home"], modifier)

    if key == "end":
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["end"]) or _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["end"], 0)
        if _matches_legacy_modifier_sequence(data, "end", modifier):
            return True
        return _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["end"], modifier)

    if key == "pageup":
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["pageUp"]) or _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["pageUp"], 0)
        if _matches_legacy_modifier_sequence(data, "pageUp", modifier):
            return True
        return _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["pageUp"], modifier)

    if key == "pagedown":
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["pageDown"]) or _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["pageDown"], 0)
        if _matches_legacy_modifier_sequence(data, "pageDown", modifier):
            return True
        return _matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["pageDown"], modifier)

    if key == "up":
        if modifier == MODIFIERS["alt"]:
            return data == "\x1bp" or _matches_kitty_sequence(data, ARROW_CODEPOINTS["up"], MODIFIERS["alt"])
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["up"]) or _matches_kitty_sequence(data, ARROW_CODEPOINTS["up"], 0)
        if _matches_legacy_modifier_sequence(data, "up", modifier):
            return True
        return _matches_kitty_sequence(data, ARROW_CODEPOINTS["up"], modifier)

    if key == "down":
        if modifier == MODIFIERS["alt"]:
            return data == "\x1bn" or _matches_kitty_sequence(data, ARROW_CODEPOINTS["down"], MODIFIERS["alt"])
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["down"]) or _matches_kitty_sequence(data, ARROW_CODEPOINTS["down"], 0)
        if _matches_legacy_modifier_sequence(data, "down", modifier):
            return True
        return _matches_kitty_sequence(data, ARROW_CODEPOINTS["down"], modifier)

    if key == "left":
        if modifier == MODIFIERS["alt"]:
            return data in ("\x1b[1;3D", "\x1bb") or (not _kitty_protocol_active and data == "\x1bB") or _matches_kitty_sequence(data, ARROW_CODEPOINTS["left"], MODIFIERS["alt"])
        if modifier == MODIFIERS["ctrl"]:
            return data == "\x1b[1;5D" or _matches_legacy_modifier_sequence(data, "left", MODIFIERS["ctrl"]) or _matches_kitty_sequence(data, ARROW_CODEPOINTS["left"], MODIFIERS["ctrl"])
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["left"]) or _matches_kitty_sequence(data, ARROW_CODEPOINTS["left"], 0)
        if _matches_legacy_modifier_sequence(data, "left", modifier):
            return True
        return _matches_kitty_sequence(data, ARROW_CODEPOINTS["left"], modifier)

    if key == "right":
        if modifier == MODIFIERS["alt"]:
            return data in ("\x1b[1;3C", "\x1bf") or (not _kitty_protocol_active and data == "\x1bF") or _matches_kitty_sequence(data, ARROW_CODEPOINTS["right"], MODIFIERS["alt"])
        if modifier == MODIFIERS["ctrl"]:
            return data == "\x1b[1;5C" or _matches_legacy_modifier_sequence(data, "right", MODIFIERS["ctrl"]) or _matches_kitty_sequence(data, ARROW_CODEPOINTS["right"], MODIFIERS["ctrl"])
        if modifier == 0:
            return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["right"]) or _matches_kitty_sequence(data, ARROW_CODEPOINTS["right"], 0)
        if _matches_legacy_modifier_sequence(data, "right", modifier):
            return True
        return _matches_kitty_sequence(data, ARROW_CODEPOINTS["right"], modifier)

    if key in ("f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12"):
        if modifier != 0:
            return False
        return _matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES[key])

    # Single letter/digit/symbol keys
    if len(key) == 1 and (("a" <= key <= "z") or _is_digit_key(key) or key in SYMBOL_KEYS):
        codepoint = ord(key)
        raw_ctrl = _raw_ctrl_char(key)
        is_letter = "a" <= key <= "z"
        is_digit = _is_digit_key(key)

        if modifier == MODIFIERS["ctrl"] + MODIFIERS["alt"] and not _kitty_protocol_active and raw_ctrl:
            if data == f"\x1b{raw_ctrl}":
                return True
        if modifier == MODIFIERS["alt"] and not _kitty_protocol_active and (is_letter or is_digit):
            if data == f"\x1b{key}":
                return True
        if modifier == MODIFIERS["ctrl"]:
            if raw_ctrl and data == raw_ctrl:
                return True
            return _matches_kitty_sequence(data, codepoint, MODIFIERS["ctrl"]) or _matches_printable_modify_other_keys(data, codepoint, MODIFIERS["ctrl"])
        if modifier == MODIFIERS["shift"] + MODIFIERS["ctrl"]:
            return _matches_kitty_sequence(data, codepoint, MODIFIERS["shift"] + MODIFIERS["ctrl"]) or _matches_printable_modify_other_keys(data, codepoint, MODIFIERS["shift"] + MODIFIERS["ctrl"])
        if modifier == MODIFIERS["shift"]:
            if is_letter and data == key.upper():
                return True
            return _matches_kitty_sequence(data, codepoint, MODIFIERS["shift"]) or _matches_printable_modify_other_keys(data, codepoint, MODIFIERS["shift"])
        if modifier != 0:
            return _matches_kitty_sequence(data, codepoint, modifier) or _matches_printable_modify_other_keys(data, codepoint, modifier)
        return data == key or _matches_kitty_sequence(data, codepoint, 0)

    return False


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------


def _format_key_name(key_name: str, modifier: int) -> str | None:
    mods: list[str] = []
    effective = modifier & ~LOCK_MASK
    supported = MODIFIERS["shift"] | MODIFIERS["ctrl"] | MODIFIERS["alt"] | MODIFIERS["super"]
    if (effective & ~supported) != 0:
        return None
    if effective & MODIFIERS["shift"]:
        mods.append("shift")
    if effective & MODIFIERS["ctrl"]:
        mods.append("ctrl")
    if effective & MODIFIERS["alt"]:
        mods.append("alt")
    if effective & MODIFIERS["super"]:
        mods.append("super")
    return "+".join(mods + [key_name]) if mods else key_name


def _format_parsed_key(codepoint: int, modifier: int, base_layout_key: int | None = None) -> str | None:
    normalized_cp = _normalize_kitty_functional_codepoint(codepoint)
    identity_cp = _normalize_shifted_letter(normalized_cp, modifier)
    is_latin = 97 <= identity_cp <= 122
    is_digit = 48 <= identity_cp <= 57
    is_symbol = chr(identity_cp) in SYMBOL_KEYS if 32 <= identity_cp <= 126 else False
    effective_cp = identity_cp if (is_latin or is_digit or is_symbol) else (base_layout_key or identity_cp)

    key_name: str | None = None
    if effective_cp == CODEPOINTS["escape"]:
        key_name = "escape"
    elif effective_cp == CODEPOINTS["tab"]:
        key_name = "tab"
    elif effective_cp in (CODEPOINTS["enter"], CODEPOINTS["kpEnter"]):
        key_name = "enter"
    elif effective_cp == CODEPOINTS["space"]:
        key_name = "space"
    elif effective_cp == CODEPOINTS["backspace"]:
        key_name = "backspace"
    elif effective_cp == FUNCTIONAL_CODEPOINTS["delete"]:
        key_name = "delete"
    elif effective_cp == FUNCTIONAL_CODEPOINTS["insert"]:
        key_name = "insert"
    elif effective_cp == FUNCTIONAL_CODEPOINTS["home"]:
        key_name = "home"
    elif effective_cp == FUNCTIONAL_CODEPOINTS["end"]:
        key_name = "end"
    elif effective_cp == FUNCTIONAL_CODEPOINTS["pageUp"]:
        key_name = "pageUp"
    elif effective_cp == FUNCTIONAL_CODEPOINTS["pageDown"]:
        key_name = "pageDown"
    elif effective_cp == ARROW_CODEPOINTS["up"]:
        key_name = "up"
    elif effective_cp == ARROW_CODEPOINTS["down"]:
        key_name = "down"
    elif effective_cp == ARROW_CODEPOINTS["left"]:
        key_name = "left"
    elif effective_cp == ARROW_CODEPOINTS["right"]:
        key_name = "right"
    elif 48 <= effective_cp <= 57:
        key_name = chr(effective_cp)
    elif 97 <= effective_cp <= 122:
        key_name = chr(effective_cp)
    elif chr(effective_cp) in SYMBOL_KEYS:
        key_name = chr(effective_cp)

    if not key_name:
        return None
    return _format_key_name(key_name, modifier)


def parse_key(data: str) -> str | None:
    kitty = parse_kitty_sequence(data)
    if kitty:
        return _format_parsed_key(kitty.codepoint, kitty.modifier, kitty.base_layout_key)

    mok = parse_modify_other_keys_sequence(data)
    if mok:
        return _format_parsed_key(mok.codepoint, mok.modifier)

    if _kitty_protocol_active:
        if data in ("\x1b\r", "\n"):
            return "shift+enter"

    legacy = LEGACY_SEQUENCE_KEY_IDS.get(data)
    if legacy:
        return legacy

    if data == "\x1b":
        return "escape"
    if data == "\x1c":
        return "ctrl+\\"
    if data == "\x1d":
        return "ctrl+]"
    if data == "\x1f":
        return "ctrl+-"
    if data == "\x1b\x1b":
        return "ctrl+alt+["
    if data == "\x1b\x1c":
        return "ctrl+alt+\\"
    if data == "\x1b\x1d":
        return "ctrl+alt+]"
    if data == "\x1b\x1f":
        return "ctrl+alt+-"
    if data == "\t":
        return "tab"
    if data == "\r" or data == "\x1bOM":
        return "enter"
    if data == "\n":
        return "ctrl+j"
    if data == "\x00":
        return "ctrl+space"
    if data == " ":
        return "space"
    if data == "\x7f":
        return "backspace"
    if data == "\x08":
        return "ctrl+backspace" if _is_windows_terminal_session() else "backspace"
    if data == "\x1b[Z":
        return "shift+tab"
    if not _kitty_protocol_active and data == "\x1b\r":
        return "alt+enter"
    if not _kitty_protocol_active and data == "\x1b ":
        return "alt+space"
    if data in ("\x1b\x7f", "\x1b\x08"):
        return "alt+backspace"
    if not _kitty_protocol_active and data == "\x1bB":
        return "alt+left"
    if not _kitty_protocol_active and data == "\x1bF":
        return "alt+right"
    if not _kitty_protocol_active and len(data) == 2 and data[0] == "\x1b":
        code = data[1]
        cp = ord(code)
        if 1 <= cp <= 26:
            return f"ctrl+alt+{chr(cp + 96)}"
        if 97 <= cp <= 122 or 48 <= cp <= 57:
            return f"alt+{code}"
    if data == "\x1b[A":
        return "up"
    if data == "\x1b[B":
        return "down"
    if data == "\x1b[C":
        return "right"
    if data == "\x1b[D":
        return "left"
    if data in ("\x1b[H", "\x1bOH"):
        return "home"
    if data in ("\x1b[F", "\x1bOF"):
        return "end"
    if data == "\x1b[3~":
        return "delete"
    if data == "\x1b[5~":
        return "pageUp"
    if data == "\x1b[6~":
        return "pageDown"

    if len(data) == 1:
        cp = ord(data)
        if 1 <= cp <= 26:
            return f"ctrl+{chr(cp + 96)}"
        if 32 <= cp <= 126:
            return data

    return None


# ---------------------------------------------------------------------------
# Event type checks
# ---------------------------------------------------------------------------


def is_key_release(data: str) -> bool:
    if "\x1b[200~" in data:
        return False
    return bool(re.search(r":3[utABCDH~]$", data))


def is_key_repeat(data: str) -> bool:
    if "\x1b[200~" in data:
        return False
    return bool(re.search(r":2[utABCDH~]$", data))


# ---------------------------------------------------------------------------
# Printable decoding
# ---------------------------------------------------------------------------

_KITTY_PRINTABLE_ALLOWED = MODIFIERS["shift"] | LOCK_MASK


def decode_kitty_printable(data: str) -> str | None:
    m = re.match(r"^\x1b\[(\d+)(?::(\d*))?(?::(\d+))?(?:;(\d+))?(?::(\d+))?u$", data)
    if not m:
        return None
    event_type_str = m.group(5)
    if event_type_str and int(event_type_str) == 3:  # release event — no character output
        return None
    codepoint = int(m.group(1))
    shifted_key = int(m.group(2)) if m.group(2) else None
    mod_value = int(m.group(4)) if m.group(4) else 1
    modifier = mod_value - 1
    if (modifier & ~_KITTY_PRINTABLE_ALLOWED) != 0:
        return None
    if modifier & (MODIFIERS["alt"] | MODIFIERS["ctrl"]):
        return None
    effective = codepoint
    if (modifier & MODIFIERS["shift"]) and shifted_key is not None:
        effective = shifted_key
    effective = _normalize_kitty_functional_codepoint(effective)
    if not effective or effective < 32:
        return None
    try:
        return chr(effective)
    except (ValueError, OverflowError):
        return None


def _decode_modify_other_keys_printable(data: str) -> str | None:
    parsed = parse_modify_other_keys_sequence(data)
    if not parsed:
        return None
    modifier = parsed.modifier & ~LOCK_MASK
    if (modifier & ~MODIFIERS["shift"]) != 0:
        return None
    if not parsed.codepoint or parsed.codepoint < 32:
        return None
    try:
        return chr(parsed.codepoint)
    except (ValueError, OverflowError):
        return None


def decode_printable_key(data: str) -> str | None:
    return decode_kitty_printable(data) or _decode_modify_other_keys_printable(data)
