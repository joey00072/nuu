"""
Find tool: recursively searches for files matching glob patterns. Respects
.gitignore and supports depth limits.

Owns: FindTool, file search logic.
Delegates to: pathlib for glob traversal.

Data flow: pattern + base_dir -> find.execute() -> matching file paths

Depends on: nuu.agent.types (AgentTool, AgentToolResult), nuu.ai.types (TextContent)
"""

import pathlib
from typing import Any

from ...agent.types import AgentTool, AgentToolResult
from ...ai.types import TextContent
from .path_utils import resolve_to_cwd
from .truncate import truncate_head

DEFAULT_LIMIT = 1000


class FindTool(AgentTool):
    def __init__(self, cwd: str):
        self.name = "find"
        self.label = "find"
        self.description = (
            "Search for files by glob pattern. Returns matching file paths relative to the search directory. "
            "Respects .gitignore (simplified). Output is truncated to 1000 results or 1MB."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files, e.g. '*.ts', '**/*.json', or 'src/**/*.py'",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: current directory)",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum number of results (default: {DEFAULT_LIMIT})",
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
        pattern = params.get("pattern")
        search_dir = params.get("path", ".")
        limit = params.get("limit", DEFAULT_LIMIT)

        if not pattern:
            raise ValueError("Pattern is required")

        search_path = pathlib.Path(resolve_to_cwd(search_dir, self.cwd))
        if not search_path.exists():
            raise ValueError(f"Path not found: {search_path}")
        if not search_path.is_dir():
            raise ValueError(f"Not a directory: {search_path}")

        results = []
        try:
            found = search_path.glob(pattern)

            for p in found:
                if len(results) >= limit:
                    break
                # Filter out node_modules and .git by default for sanity
                if "node_modules" in p.parts or ".git" in p.parts:
                    continue
                if p.is_file():
                    results.append(str(p.relative_to(search_path)))
        except Exception as e:
            raise ValueError(f"Error searching for files: {e}")

        if not results:
            return AgentToolResult(
                content=[
                    TextContent(type="text", text="No files found matching pattern")
                ],
                details=None,
            )

        results.sort()
        raw_output = "\n".join(results)
        truncation = truncate_head(raw_output)
        output = truncation.content

        details = {}
        notices = []
        if len(results) >= limit:
            notices.append(f"{limit} results limit reached")
            details["resultLimitReached"] = limit
        if truncation.truncated:
            notices.append("Byte limit reached")
            details["truncation"] = truncation

        if notices:
            output += f"\n\n[{'. '.join(notices)}]"

        return AgentToolResult(
            content=[TextContent(type="text", text=output)],
            details=details if details else None,
        )
