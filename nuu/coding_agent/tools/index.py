"""
Tool factory: creates and registers all built-in coding agent tools as
AgentTool instances. Central place to add or remove available tools.

Owns: create_all_tools() factory function.
Delegates to: each tool module for instantiation.

Depends on: all nuu.coding_agent.tools submodules
"""

from .ls import LsTool
from .read import ReadTool
from .write import WriteTool
from .bash import BashTool
from .edit import EditTool
from .find import FindTool
from .grep import GrepTool
from ...agent.types import AgentTool


def create_all_tools(cwd: str) -> list[AgentTool]:
    return [
        LsTool(cwd=cwd),
        ReadTool(cwd=cwd),
        WriteTool(cwd=cwd),
        BashTool(cwd=cwd),
        EditTool(cwd=cwd),
        FindTool(cwd=cwd),
        GrepTool(cwd=cwd),
    ]
