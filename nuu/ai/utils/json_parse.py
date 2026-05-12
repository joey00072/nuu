"""
Streaming JSON parser for incrementally parsing tool call arguments as they
arrive via SSE chunks. Tolerates partial/incomplete JSON.

Owns: parse_streaming_json().
Delegates to: json for final parsing.

Depends on: standard library only (json, re)
"""

from __future__ import annotations

import json
from typing import Any

_VALID_JSON_ESCAPES = frozenset({'"', "\\", "/", "b", "f", "n", "r", "t", "u"})


def _is_control_character(char: str) -> bool:
    return 0x00 <= ord(char) <= 0x1F


def _escape_control_character(char: str) -> str:
    switch = {
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    if char in switch:
        return switch[char]
    return f"\\u{ord(char):04x}"


def repair_json(text: str) -> str:
    repaired: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        char = text[i]
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            i += 1
            continue

        if char == '"':
            repaired.append(char)
            in_string = False
            i += 1
            continue

        if char == "\\":
            if i + 1 >= len(text):
                repaired.append("\\\\")
                i += 1
                continue
            next_char = text[i + 1]
            if next_char == "u":
                unicode_digits = text[i + 2 : i + 6]
                if len(unicode_digits) == 4 and all(
                    c in "0123456789abcdefABCDEF" for c in unicode_digits
                ):
                    repaired.append(f"\\u{unicode_digits}")
                    i += 6
                    continue
            if next_char in _VALID_JSON_ESCAPES:
                repaired.append(f"\\{next_char}")
                i += 2
                continue
            repaired.append("\\\\")
            i += 1
            continue

        if _is_control_character(char):
            repaired.append(_escape_control_character(char))
        else:
            repaired.append(char)
        i += 1

    return "".join(repaired)


def parse_json_with_repair(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        repaired = repair_json(text)
        if repaired != text:
            return json.loads(repaired)
        raise


def parse_streaming_json(partial_json: str) -> dict[str, Any]:
    if not partial_json or partial_json.strip() == "":
        return {}
    try:
        result = parse_json_with_repair(partial_json)
        if isinstance(result, dict):
            return result
        return {}
    except (json.JSONDecodeError, ValueError):
        try:
            decoder = json.JSONDecoder()
            result, _ = decoder.raw_decode(partial_json)
            if isinstance(result, dict):
                return result
            return {}
        except (json.JSONDecodeError, ValueError):
            try:
                repaired = repair_json(partial_json)
                decoder = json.JSONDecoder()
                result, _ = decoder.raw_decode(repaired)
                if isinstance(result, dict):
                    return result
                return {}
            except (json.JSONDecodeError, ValueError):
                return {}
