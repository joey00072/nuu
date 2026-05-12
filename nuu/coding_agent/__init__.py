"""
Public API surface for the coding agent package. Re-exports session management,
configuration, skills, tools, migrations, and the main entry point.

Owns: the canonical import path for all coding_agent public symbols.
Delegates to: each submodule for implementation.

Depends on: nuu.coding_agent subpackages (cli, core, tools, utils)
"""

from . import cli
from . import config
from . import core
from .main import main as main_entry
from . import migrations
from . import session
from . import session_manager
from . import skills
from . import tools
from . import utils

from .session import AgentSession
from .session_manager import SessionManager, SessionEntry, SessionHeader
from .config import (
    VERSION,
    get_agent_dir,
    get_config_dir,
    get_sessions_dir,
    get_skills_dir,
    get_cache_dir,
    get_auth_file,
    get_settings_file,
)
from .migrations import run_migrations, Migration
from .skills import (
    Skill,
    load_skill_from_file,
    load_skills_from_dir,
    format_skills_for_prompt,
)
from .tools.index import create_all_tools

__all__ = [
    "cli",
    "config",
    "core",
    "main_entry",
    "migrations",
    "session",
    "session_manager",
    "skills",
    "tools",
    "utils",
    "AgentSession",
    "SessionManager",
    "SessionEntry",
    "SessionHeader",
    "VERSION",
    "get_agent_dir",
    "get_config_dir",
    "get_sessions_dir",
    "get_skills_dir",
    "get_cache_dir",
    "get_auth_file",
    "get_settings_file",
    "main_entry",
    "run_migrations",
    "Migration",
    "Skill",
    "load_skill_from_file",
    "load_skills_from_dir",
    "format_skills_for_prompt",
    "create_all_tools",
]
