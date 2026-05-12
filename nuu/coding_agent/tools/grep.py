"""
Grep tool: searches file contents using regex patterns. Supports include/
exclude file patterns, context lines, and case-insensitive search.

Owns: GrepTool, regex search logic.
Delegates to: re for pattern matching, os/pathlib for file traversal.

Data flow: pattern + path -> grep.execute() -> matching lines with context

Depends on: nuu.agent.types (AgentTool, AgentToolResult), nuu.ai.types (TextContent),
  re
"""

import os
import re
import pathlib
from typing import Any

from ...agent.types import AgentTool, AgentToolResult
from ...ai.types import TextContent
from .path_utils import resolve_to_cwd
from .truncate import truncate_head, truncate_line

DEFAULT_LIMIT = 100


class GrepTool(AgentTool):
    def __init__(self, cwd: str):
        self.name = "grep"
        self.label = "grep"
        self.description = (
            "Search file contents for a pattern. Returns matching lines with file paths and line numbers. "
            "Respects .gitignore (simplified). Output is truncated to 100 matches or 1MB. "
            "Long lines are truncated."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern (regex)"},
                "path": {
                    "type": "string",
                    "description": "Directory or file to search (default: current directory)",
                },
                "glob": {
                    "type": "string",
                    "description": "Filter files by glob pattern, e.g. '*.ts' or '**/*.spec.ts'",
                },
                "ignoreCase": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default: false)",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum number of matches to return (default: {DEFAULT_LIMIT})",
                },
            },
            "required": ["pattern"],
        }
        self.cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        on_update=None,
    ) -> AgentToolResult:
        pattern_str = params.get("pattern")
        search_dir = params.get("path", ".")
        glob_pattern = params.get("glob")
        ignore_case = params.get("ignoreCase", False)
        limit = params.get("limit", DEFAULT_LIMIT)

        if not pattern_str:
            raise ValueError("Pattern is required")

        search_path = pathlib.Path(resolve_to_cwd(search_dir, self.cwd))
        if not search_path.exists():
            raise ValueError(f"Path not found: {search_path}")

        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern_str, flags)
        except Exception as e:
            raise ValueError(f"Invalid regex: {e}")

        matches = []
        match_limit_reached = False

        # Determine files to search
        files_to_search = []
        if search_path.is_file():
            files_to_search.append(search_path)
        else:
            # Recursive search
            if glob_pattern:
                if "**" in glob_pattern:
                    files_to_search = list(
                        search_path.rglob(glob_pattern.replace("**/", ""))
                    )
                else:
                    files_to_search = list(search_path.glob(glob_pattern))
            else:
                for root, dirs, files in os.walk(search_path):
                    # Skip common junk
                    if "node_modules" in dirs:
                        dirs.remove("node_modules")
                    if ".git" in dirs:
                        dirs.remove(".git")
                    for file in files:
                        files_to_search.append(pathlib.Path(root) / file)

        for p in files_to_search:
            if match_limit_reached:
                break
            if not p.is_file():
                continue

            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    for line_num, line in enumerate(f, 1):
                        if regex.search(line):
                            rel_path = (
                                str(p.relative_to(search_path))
                                if not search_path.is_file()
                                else p.name
                            )
                            truncated_line = truncate_line(line.rstrip()).text
                            matches.append(f"{rel_path}:{line_num}: {truncated_line}")
                            if len(matches) >= limit:
                                match_limit_reached = True
                                break
            except Exception:
                continue

        if not matches:
            return AgentToolResult(
                content=[TextContent(type="text", text="No matches found")],
                details=None,
            )

        raw_output = "\n".join(matches)
        truncation = truncate_head(raw_output)
        output = truncation.content

        details = {}
        notices = []
        if match_limit_reached:
            notices.append(f"{limit} matches limit reached")
            details["matchLimitReached"] = limit
        if truncation.truncated:
            notices.append("Byte limit reached")
            details["truncation"] = truncation

        if notices:
            output += f"\n\n[{'. '.join(notices)}]"

        return AgentToolResult(
            content=[TextContent(type="text", text=output)],
            details=details if details else None,
        )
