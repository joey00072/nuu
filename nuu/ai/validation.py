"""
JSON Schema validation for tool call arguments using jsonschema library.
Provides validate_tool_call() (lookup + validate) and validate_tool_arguments().

Owns: tool call validation logic, error formatting.
Delegates to: jsonschema.validate for schema compliance checking.

Depends on: nuu.ai.types (Tool, ToolCall), jsonschema
"""

import json
from typing import Any

from jsonschema import validate, ValidationError

from .types import Tool, ToolCall


def validate_tool_arguments(tool: Tool, tool_call: ToolCall) -> dict[str, Any]:
    args = tool_call.arguments
    try:
        validate(instance=args, schema=tool.parameters)
        return args
    except ValidationError as e:
        # Format error message similar to Pi
        path = ".".join(str(p) for p in e.path) or "root"
        error_msg = (
            f'Validation failed for tool "{tool_call.name}":\n'
            f"  - {path}: {e.message}\n\n"
            f"Received arguments:\n{json.dumps(tool_call.arguments, indent=2)}"
        )
        raise ValueError(error_msg) from e


def validate_tool_call(tools: list[Tool], tool_call: ToolCall) -> dict[str, Any]:
    tool = next((t for t in tools if t.name == tool_call.name), None)
    if not tool:
        raise ValueError(f'Tool "{tool_call.name}" not found')
    return validate_tool_arguments(tool, tool_call)
