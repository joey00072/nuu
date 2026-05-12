"""
Diagnostic event types and logging for assistant message processing.
Captures warnings, errors, and debug info emitted during streaming.

Owns: diagnostic TypedDict definitions.
Delegates to: caller for logging/storage of diagnostics.

Depends on: standard library only (typing)
"""

from __future__ import annotations

import traceback
from typing import Any, TypedDict

from nuu.ai.types import AssistantMessage, AssistantMessageDiagnostic


class DiagnosticErrorInfo(TypedDict):
    name: str | None
    message: str
    stack: str | None
    code: str | int | None


def format_thrown_value(value: Any) -> str:
    if isinstance(value, BaseException):
        return str(value) or type(value).__name__
    if isinstance(value, str):
        return value
    return str(value)


def extract_diagnostic_error(error: Exception) -> DiagnosticErrorInfo:
    code = getattr(error, "code", None)
    tb: str | None = None
    if error.__traceback__ is not None:
        tb = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
    return DiagnosticErrorInfo(
        name=type(error).__name__,
        message=str(error) or type(error).__name__,
        stack=tb,
        code=code if isinstance(code, (str, int)) else None,
    )


def create_assistant_message_diagnostic(
    type: str,
    error: Exception,
    details: Any = None,
) -> AssistantMessageDiagnostic:
    info = extract_diagnostic_error(error)
    return AssistantMessageDiagnostic(
        type=type,
        message=info["message"],
        details=details,
    )


def append_assistant_message_diagnostic(
    message: AssistantMessage,
    diagnostic: AssistantMessageDiagnostic,
) -> None:
    if message.diagnostics is None:
        message.diagnostics = []
    message.diagnostics.append(diagnostic)
