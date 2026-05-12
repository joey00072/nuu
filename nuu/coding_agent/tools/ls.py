"""
Ls tool: lists directory contents with file type annotations and size info.
Provides a quick overview of a directory's structure.

Owns: LsTool, directory listing logic.
Delegates to: os, pathlib for directory inspection.

Data flow: path -> ls.execute() -> formatted directory listing

Depends on: nuu.agent.types (AgentTool, AgentToolResult), nuu.ai.types (TextContent)
"""

import os
from typing import Any

from ...agent.types import AgentTool, AgentToolResult
from ...ai.types import TextContent
from .path_utils import resolve_to_cwd
from .truncate import truncate_head, DEFAULT_MAX_BYTES

DEFAULT_LIMIT = 500


class LsTool(AgentTool):
    def __init__(self, cwd: str):
        self.name = "ls"
        self.label = "ls"
        self.description = (
            "List directory contents. Returns entries sorted alphabetically, "
            "with '/' suffix for directories. Includes dotfiles. "
            f"Output is truncated to {DEFAULT_LIMIT} entries or "
            f"{DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first)."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list (default: current directory)",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum number of entries to return (default: {DEFAULT_LIMIT})",
                },
            },
        }
        self.cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        on_update=None,
    ) -> AgentToolResult:
        path = params.get("path", ".")
        limit = params.get("limit", DEFAULT_LIMIT)

        dir_path = resolve_to_cwd(path, self.cwd)

        if not os.path.exists(dir_path):
            raise ValueError(f"Path not found: {dir_path}")

        if not os.path.isdir(dir_path):
            raise ValueError(f"Not a directory: {dir_path}")

        try:
            entries = os.listdir(dir_path)
        except Exception as e:
            raise ValueError(f"Cannot read directory: {e}")

        entries.sort(key=str.lower)

        results = []
        entry_limit_reached = False
        for entry in entries:
            if len(results) >= limit:
                entry_limit_reached = True
                break

            full_path = os.path.join(dir_path, entry)
            suffix = "/" if os.path.isdir(full_path) else ""
            results.append(entry + suffix)

        if not results:
            return AgentToolResult(
                content=[TextContent(type="text", text="(empty directory)")],
                details=None,
            )

        raw_output = "\n".join(results)
        truncation = truncate_head(raw_output)
        output = truncation.content

        notices = []
        if entry_limit_reached:
            notices.append(f"{limit} entries limit reached")
        if truncation.truncated:
            notices.append("Byte limit reached")

        if notices:
            output += f"\n\n[{'. '.join(notices)}]"

        details = {}
        if entry_limit_reached:
            details["entryLimitReached"] = limit
        if truncation.truncated:
            details["truncation"] = truncation

        return AgentToolResult(
            content=[TextContent(type="text", text=output)],
            details=details if details else None,
        )
