"""
Edit tool: applies exact-string replacements to files. Uses difflib to show
changes and validates that the old string exists before applying.

Owns: EditTool, diff computation, file modification.
Delegates to: os, pathlib for file I/O, difflib for context diff.

Data flow: file_path + old_string + new_string -> edit() -> modified file + diff

Depends on: nuu.agent.types (AgentTool, AgentToolResult), nuu.ai.types (TextContent),
  difflib
"""

import os
import difflib
from typing import Any
from pydantic import BaseModel

from ...agent.types import AgentTool, AgentToolResult
from ...ai.types import TextContent
from .path_utils import resolve_to_cwd


class EditItem(BaseModel):
    oldText: str
    newText: str


class EditTool(AgentTool):
    def __init__(self, cwd: str):
        self.name = "edit"
        self.label = "edit"
        self.description = (
            "Edit a single file using exact text replacement. Every edits[].oldText must match a "
            "unique, non-overlapping region of the original file. If two changes affect the same "
            "block or nearby lines, merge them into one edit instead of emitting overlapping edits."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oldText": {
                                "type": "string",
                                "description": "Exact text to replace",
                            },
                            "newText": {
                                "type": "string",
                                "description": "Replacement text",
                            },
                        },
                        "required": ["oldText", "newText"],
                    },
                    "description": "List of edits to apply",
                },
            },
            "required": ["path", "edits"],
        }
        self.cwd = cwd

    def prepare_arguments(self, args: dict[str, Any]) -> dict[str, Any]:
        # Handle cases where edits might be a string or legacy oldText/newText
        if "oldText" in args and "newText" in args and "edits" not in args:
            args["edits"] = [{"oldText": args["oldText"], "newText": args["newText"]}]
        return args

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        on_update=None,
    ) -> AgentToolResult:
        path = params.get("path")
        edits_data = params.get("edits", [])

        if not path:
            raise ValueError("Path is required")
        if not edits_data:
            raise ValueError("Edits are required")

        absolute_path = resolve_to_cwd(path, self.cwd)
        if not os.path.exists(absolute_path):
            raise ValueError(f"File not found: {path}")

        with open(absolute_path, "r", encoding="utf-8") as f:
            content = f.read()

        original_content = content

        # Apply edits in order of occurrence to avoid offset issues
        # Or better: check for uniqueness and then apply in reverse order of index

        edits = [EditItem(**e) for e in edits_data]
        matches = []
        for i, edit in enumerate(edits):
            if not edit.oldText:
                raise ValueError(f"edits[{i}].oldText must not be empty")

            count = content.count(edit.oldText)
            if count == 0:
                raise ValueError(
                    f"Could not find exact match for edits[{i}].oldText in {path}"
                )
            if count > 1:
                raise ValueError(
                    f"Found {count} occurrences of edits[{i}].oldText in {path}. Must be unique."
                )

            index = content.find(edit.oldText)
            matches.append(
                {
                    "index": index,
                    "length": len(edit.oldText),
                    "newText": edit.newText,
                    "editIndex": i,
                }
            )

        # Sort matches by index and check for overlaps
        matches.sort(key=lambda x: x["index"])
        for i in range(1, len(matches)):
            prev = matches[i - 1]
            curr = matches[i]
            if prev["index"] + prev["length"] > curr["index"]:
                raise ValueError(
                    f"edits[{prev['editIndex']}] and edits[{curr['editIndex']}] overlap in {path}"
                )

        # Apply replacements in reverse order
        new_content = content
        for match in reversed(matches):
            new_content = (
                new_content[: match["index"]]
                + match["newText"]
                + new_content[match["index"] + match["length"] :]
            )

        if new_content == original_content:
            raise ValueError(f"No changes made to {path}")

        with open(absolute_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        # Generate a simple diff
        diff = list(
            difflib.unified_diff(
                original_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        diff_str = "".join(diff)

        return AgentToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"Successfully applied {len(edits)} edit(s) to {path}.",
                )
            ],
            details={"diff": diff_str},
        )
