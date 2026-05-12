"""
Bash tool: executes shell commands in a persistent session. Runs commands
with timeout and capture, emitting progress updates for long-running tasks.

Owns: BashTool, command execution, output streaming.
Delegates to: asyncio.subprocess for process execution.

Data flow: command string -> bash.execute() -> stdout/stderr -> ToolResult

Depends on: nuu.agent.types (AgentTool, AgentToolResult), nuu.ai.types (TextContent)
"""

import asyncio
import os
import tempfile
from typing import Any

from ...agent.types import AgentTool, AgentToolResult
from ...ai.types import TextContent
from .truncate import truncate_tail, DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, format_size


class BashTool(AgentTool):
    def __init__(self, cwd: str):
        self.name = "bash"
        self.label = "bash"
        self.description = (
            "Execute a bash command in the current working directory. "
            "Returns stdout and stderr. Output is truncated to last "
            f"{DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB "
            "(whichever is hit first). If truncated, full output is saved to "
            "a temp file. Optionally provide a timeout in seconds."
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Bash command to execute"},
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (optional, no default timeout)",
                },
            },
            "required": ["command"],
        }
        self.cwd = cwd

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        on_update=None,
    ) -> AgentToolResult:
        command = params["command"]
        timeout = params.get("timeout")

        if not os.path.exists(self.cwd):
            raise ValueError(f"Working directory does not exist: {self.cwd}")

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.cwd,
        )

        try:
            if timeout:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            else:
                stdout, _ = await process.communicate()
        except asyncio.TimeoutError:
            process.kill()
            stdout, _ = await process.communicate()
            raise ValueError(
                f"Command timed out after {timeout} seconds. Output so far:\n{stdout.decode('utf-8', errors='ignore')}"
            )
        except Exception as e:
            process.kill()
            raise ValueError(f"Command failed: {e}")

        output_str = stdout.decode("utf-8", errors="ignore")
        truncation = truncate_tail(output_str)
        text = truncation.content

        full_output_path = None
        if truncation.truncated:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".log", delete=False, prefix="nuu-bash-"
            ) as f:
                f.write(output_str)
                full_output_path = f.name

            start_line = truncation.total_lines - truncation.output_lines + 1
            end_line = truncation.total_lines

            if truncation.last_line_partial:
                text += f"\n\n[Showing last {format_size(truncation.output_bytes)} of line {end_line}. Full output: {full_output_path}]"
            elif truncation.truncated_by == "lines":
                text += f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines}. Full output: {full_output_path}]"
            else:
                text += f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines} ({format_size(DEFAULT_MAX_BYTES)} limit). Full output: {full_output_path}]"

        if not text and process.returncode == 0:
            text = "(no output)"

        if process.returncode != 0:
            text += f"\n\n[Command exited with code {process.returncode}]"
            # Pi throws Error for non-zero exit code if it's considered a failure
            # but usually it just returns the output.
            # In AgentLoop, we might want to know if it's an error.

        return AgentToolResult(
            content=[TextContent(type="text", text=text)],
            details={"fullOutputPath": full_output_path} if full_output_path else None,
        )
