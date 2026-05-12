"""
Header conversion utilities. Normalizes various header container types (dict,
httpx.Headers, etc.) into plain dict[str, str].

Owns: headers_to_dict() and related helpers.
Delegates to: caller for source of header objects.

Depends on: standard library only (typing)
"""

from typing import Any


def headers_to_dict(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        result[str(key)] = str(value)
    return result
