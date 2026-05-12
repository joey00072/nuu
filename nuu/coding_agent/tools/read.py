"""
Read tool: reads file contents with optional line offset and limit. Supports
reading images and PDFs (returned as attachments) and text files.

Owns: ReadTool, file reading with range support.
Delegates to: os, pathlib for file I/O, image/pdf detection for attachments.

Data flow: file_path + offset + limit -> read.execute() -> file content or attachment

Depends on: nuu.agent.types (AgentTool, AgentToolResult), nuu.ai.types (TextContent)
"""

import os
from typing import Any, Optional

from ...agent.types import AgentTool, AgentToolResult
from ...ai.types import TextContent
from .path_utils import resolve_read_path
from .truncate import truncate_head, DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, format_size


class ReadTool(AgentTool):
    def __init__(self, cwd: str):
        self.name = "read"
        self.label = "read"
        self.description = (
            "Read the contents of a file. For text files, output is truncated "
            f"to {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB "
            "(whichever is hit first). Use offset/limit for large files."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (relative or absolute)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read",
                },
            },
            "required": ["path"],
        }
        self.cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        on_update=None,
    ) -> AgentToolResult:
        path = params["path"]
        offset = params.get("offset")
        limit = params.get("limit")

        absolute_path = resolve_read_path(path, self.cwd)

        if not os.path.exists(absolute_path):
            raise ValueError(f"Path not found: {absolute_path}")

        if not os.path.isfile(absolute_path):
            raise ValueError(f"Not a file: {absolute_path}")

        try:
            with open(absolute_path, "r", encoding="utf-8") as f:
                text_content = f.read()
        except Exception as e:
            raise ValueError(f"Cannot read file: {e}")

        all_lines = text_content.split("\n")
        total_file_lines = len(all_lines)

        start_line = max(0, offset - 1) if offset else 0
        start_line_display = start_line + 1

        if start_line >= len(all_lines):
            raise ValueError(
                f"Offset {offset} is beyond end of file ({len(all_lines)} lines total)"
            )

        user_limited_lines: Optional[int] = None
        if limit is not None:
            end_line = min(start_line + limit, len(all_lines))
            selected_content = "\n".join(all_lines[start_line:end_line])
            user_limited_lines = end_line - start_line
        else:
            selected_content = "\n".join(all_lines[start_line:])

        truncation = truncate_head(selected_content)
        output_text = ""
        details = {}

        if truncation.first_line_exceeds_limit:
            first_line_size = format_size(len(all_lines[start_line].encode("utf-8")))
            output_text = (
                f"[Line {start_line_display} is {first_line_size}, "
                f"exceeds {format_size(DEFAULT_MAX_BYTES)} limit. "
                f"Use offset/limit to read smaller chunks.]"
            )
            details["truncation"] = truncation
        elif truncation.truncated:
            end_line_display = start_line_display + truncation.output_lines - 1
            next_offset = end_line_display + 1
            output_text = truncation.content
            if truncation.truncated_by == "lines":
                output_text += (
                    f"\n\n[Showing lines {start_line_display}-{end_line_display} "
                    f"of {total_file_lines}. Use offset={next_offset} to continue.]"
                )
            else:
                output_text += (
                    f"\n\n[Showing lines {start_line_display}-{end_line_display} "
                    f"of {total_file_lines} ({format_size(DEFAULT_MAX_BYTES)} limit). "
                    f"Use offset={next_offset} to continue.]"
                )
            details["truncation"] = truncation
        elif user_limited_lines is not None and start_line + user_limited_lines < len(
            all_lines
        ):
            remaining = len(all_lines) - (start_line + user_limited_lines)
            next_offset = start_line + user_limited_lines + 1
            output_text = (
                f"{truncation.content}\n\n[{remaining} more lines in file. "
                f"Use offset={next_offset} to continue.]"
            )
        else:
            output_text = truncation.content

        return AgentToolResult(
            content=[TextContent(type="text", text=output_text)],
            details=details if details else None,
        )
