"""
Write tool: creates or overwrites files with given content. Creates parent
directories as needed and validates write permissions.

Owns: WriteTool, file creation/overwrite logic.
Delegates to: os, pathlib for file I/O and directory creation.

Data flow: file_path + content -> write.execute() -> created/updated file

Depends on: nuu.agent.types (AgentTool, AgentToolResult), nuu.ai.types (TextContent)
"""

import os
from typing import Any

from ...agent.types import AgentTool, AgentToolResult
from ...ai.types import TextContent
from .path_utils import resolve_to_cwd


class WriteTool(AgentTool):
    def __init__(self, cwd: str):
        self.name = "write"
        self.label = "write"
        self.description = (
            "Write content to a file. Creates the file if it doesn't exist, "
            "overwrites if it does. Automatically creates parent directories."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write (relative or absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        }
        self.cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        on_update=None,
    ) -> AgentToolResult:
        path = params["path"]
        content = params["content"]

        absolute_path = resolve_to_cwd(path, self.cwd)
        dir_path = os.path.dirname(absolute_path)

        try:
            os.makedirs(dir_path, exist_ok=True)
            with open(absolute_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            raise ValueError(f"Cannot write file: {e}")

        return AgentToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"Successfully wrote {len(content.encode('utf-8'))} bytes to {path}",
                )
            ],
            details=None,
        )
