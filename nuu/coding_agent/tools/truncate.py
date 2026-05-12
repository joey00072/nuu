"""
Truncate tool: truncates a file to a specified number of lines from the start
or end. Used to reduce large files to manageable context windows.

Owns: TruncateTool, file truncation logic.
Delegates to: os, pathlib for file I/O.

Data flow: file_path + mode + max_lines -> truncated file content

Depends on: nuu.agent.types (AgentTool, AgentToolResult), nuu.ai.types (TextContent)
"""

from dataclasses import dataclass
from typing import Literal


DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50KB
GREP_MAX_LINE_LENGTH = 500


@dataclass
class TruncationResult:
    content: str
    truncated: bool
    truncated_by: Literal["lines", "bytes"] | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int


@dataclass
class LineTruncationResult:
    text: str
    was_truncated: bool


def truncate_line(
    line: str, max_length: int = GREP_MAX_LINE_LENGTH
) -> LineTruncationResult:
    if len(line) <= max_length:
        return LineTruncationResult(text=line, was_truncated=False)
    return LineTruncationResult(text=line[:max_length] + "...", was_truncated=True)


def format_size(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{bytes_count}B"
    elif bytes_count < 1024 * 1024:
        return f"{(bytes_count / 1024):.1f}KB"
    else:
        return f"{(bytes_count / (1024 * 1024)):.1f}MB"


def truncate_head(
    content: str, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = DEFAULT_MAX_BYTES
) -> TruncationResult:
    total_bytes = len(content.encode("utf-8"))
    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    first_line_bytes = len(lines[0].encode("utf-8"))
    if first_line_bytes > max_bytes:
        return TruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=0,
            output_bytes=0,
            last_line_partial=False,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output_lines_arr = []
    output_bytes_count = 0
    truncated_by = "lines"

    for i, line in enumerate(lines):
        if i >= max_lines:
            truncated_by = "lines"
            break

        line_bytes = len(line.encode("utf-8")) + (1 if i > 0 else 0)

        if output_bytes_count + line_bytes > max_bytes:
            truncated_by = "bytes"
            break

        output_lines_arr.append(line)
        output_bytes_count += line_bytes

    if len(output_lines_arr) >= max_lines and output_bytes_count <= max_bytes:
        truncated_by = "lines"

    output_content = "\n".join(output_lines_arr)
    final_output_bytes = len(output_content.encode("utf-8"))

    return TruncationResult(
        content=output_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines_arr),
        output_bytes=final_output_bytes,
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tail(
    content: str, max_lines: int = DEFAULT_MAX_LINES, max_bytes: int = DEFAULT_MAX_BYTES
) -> TruncationResult:
    total_bytes = len(content.encode("utf-8"))
    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output_lines_arr = []
    output_bytes_count = 0
    truncated_by = "lines"
    last_line_partial = False

    for i in range(len(lines) - 1, -1, -1):
        if len(output_lines_arr) >= max_lines:
            truncated_by = "lines"
            break

        line = lines[i]
        line_bytes = len(line.encode("utf-8")) + (1 if output_lines_arr else 0)

        if output_bytes_count + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not output_lines_arr:
                truncated_line = _truncate_string_to_bytes_from_end(line, max_bytes)
                output_lines_arr.insert(0, truncated_line)
                output_bytes_count = len(truncated_line.encode("utf-8"))
                last_line_partial = True
            break

        output_lines_arr.insert(0, line)
        output_bytes_count += line_bytes

    if len(output_lines_arr) >= max_lines and output_bytes_count <= max_bytes:
        truncated_by = "lines"

    output_content = "\n".join(output_lines_arr)
    final_output_bytes = len(output_content.encode("utf-8"))

    return TruncationResult(
        content=output_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines_arr),
        output_bytes=final_output_bytes,
        last_line_partial=last_line_partial,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def _truncate_string_to_bytes_from_end(s: str, max_bytes: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s

    # Start from the end, skip max_bytes back
    start = len(b) - max_bytes

    # Find a valid UTF-8 boundary
    while start < len(b) and (b[start] & 0xC0) == 0x80:
        start += 1

    return b[start:].decode("utf-8", errors="ignore")
