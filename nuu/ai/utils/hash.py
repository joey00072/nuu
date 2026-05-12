"""
Fast non-cryptographic hashing utilities for content signatures and IDs.
Based on a simple 32-bit integer multiply-and-xor algorithm.

Owns: hash functions used by text_signature and content identification.
Delegates to: standard library only.

Depends on: standard library only
"""


def _imul(a: int, b: int) -> int:
    return (a * b) & 0xFFFFFFFF


def _int_to_base36(val: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    val = val & 0xFFFFFFFF
    if val == 0:
        return "0"
    result: list[str] = []
    while val > 0:
        result.append(chars[val % 36])
        val //= 36
    return "".join(reversed(result))


def short_hash(text: str) -> str:
    h1 = 0xDEADBEEF
    h2 = 0x41C6CE57
    for ch in text:
        code = ord(ch)
        h1 = _imul(h1 ^ code, 2654435761)
        h2 = _imul(h2 ^ code, 1597334677)
    h1 = _imul(h1 ^ (h1 >> 16), 2246822507) ^ _imul(h2 ^ (h2 >> 13), 3266489909)
    h2 = _imul(h2 ^ (h2 >> 16), 2246822507) ^ _imul(h1 ^ (h1 >> 13), 3266489909)
    return _int_to_base36(h2) + _int_to_base36(h1)
