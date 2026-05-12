"""
Sanitizes unicode strings by removing/replacing invalid surrogate pairs and
control characters that can cause JSON serialization errors in provider APIs.

Owns: sanitize_unicode().
Delegates to: re for pattern matching.

Depends on: standard library only (re)
"""

import re


_SURROGATE_RE = re.compile(
    r"[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]"
)


def sanitize_surrogates(text: str) -> str:
    return _SURROGATE_RE.sub("", text)
