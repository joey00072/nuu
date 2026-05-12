"""
Command-line argument parsing for the coding agent. Defines all CLI flags
(provider, model, thinking, system prompt, offline, etc.) and produces
a typed ParsedArgs dict.

Owns: parse_args(), ParsedArgs TypedDict, argument parser definition.
Delegates to: argparse for argument parsing.

Data flow: argv -> parse_args() -> ParsedArgs

Depends on: argparse, sys, typing
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, TypedDict

VALID_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")
VALID_MODES = ("text", "json", "rpc")


class ParsedArgs(TypedDict, total=False):
    command: str
    prompt: str
    provider: str
    model: str
    api_key: str
    system_prompt: str
    append_system_prompt: list[str]
    thinking: str
    continue_: bool
    resume: bool
    session: str
    fork: str
    session_dir: str
    no_session: bool
    models: list[str]
    no_tools: bool
    no_builtin_tools: bool
    tools: list[str]
    extensions: list[str]
    no_extensions: bool
    skills: list[str]
    no_skills: bool
    prompt_templates: list[str]
    no_prompt_templates: bool
    themes: list[str]
    no_themes: bool
    no_context_files: bool
    mode: str
    print: bool
    export: str
    list_models: str | bool
    verbose: bool
    offline: bool
    file_args: list[str]
    temperature: float
    max_tokens: int
    session_action: str
    session_id: str
    login_provider: str
    list_provider: str


ENV_VARS_DOC = """
Environment Variables:
  ANTHROPIC_API_KEY                - Anthropic Claude API key
  OPENAI_API_KEY                   - OpenAI GPT API key
  GEMINI_API_KEY                   - Google Gemini API key
  DEEPSEEK_API_KEY                 - DeepSeek API key
  GROQ_API_KEY                     - Groq API key
  MISTRAL_API_KEY                  - Mistral API key
  OPENROUTER_API_KEY               - OpenRouter API key
  AWS_ACCESS_KEY_ID                - AWS access key for Amazon Bedrock
  AWS_SECRET_ACCESS_KEY            - AWS secret key for Amazon Bedrock
  AWS_REGION                       - AWS region for Amazon Bedrock
  AWS_PROFILE                      - AWS profile for Amazon Bedrock
  AZURE_OPENAI_API_KEY             - Azure OpenAI API key
  AZURE_OPENAI_BASE_URL            - Azure OpenAI base URL
  AZURE_OPENAI_API_VERSION         - Azure OpenAI API version
  AZURE_OPENAI_DEPLOYMENT_NAME_MAP - Azure OpenAI model=deployment map
  XAI_API_KEY                      - xAI Grok API key
  FIREWORKS_API_KEY                - Fireworks API key
  CEREBRAS_API_KEY                 - Cerebras API key
  CLOUDFLARE_API_KEY               - Cloudflare API token
  CLOUDFLARE_ACCOUNT_ID            - Cloudflare account id
  HF_TOKEN                         - Hugging Face API token
  NUU_AGENT_DIR                    - Config directory (default: ~/.nuu/agent)
  NUU_SESSION_DIR                  - Session storage directory
  NUU_OFFLINE                      - Disable startup network operations
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nuu",
        description="nuu - AI coding assistant with read, bash, edit, write tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=ENV_VARS_DOC,
    )

    parser.add_argument("prompt", nargs="*", metavar="PROMPT", help="Initial prompt")

    parser.add_argument("--provider", metavar="NAME", help="Provider name")
    parser.add_argument(
        "--model",
        metavar="PATTERN",
        help="Model pattern or ID (supports provider/id and optional :<thinking>)",
    )
    parser.add_argument(
        "--api-key", metavar="KEY", help="API key (defaults to env vars)"
    )
    parser.add_argument("--system-prompt", metavar="TEXT", help="System prompt")
    parser.add_argument(
        "--append-system-prompt",
        metavar="TEXT",
        action="append",
        help="Append text or file contents to the system prompt (can be used multiple times)",
    )
    parser.add_argument(
        "--mode",
        metavar="MODE",
        choices=VALID_MODES,
        default="text",
        help="Output mode: text (default), json, or rpc",
    )
    parser.add_argument(
        "--print",
        "-p",
        action="store_true",
        help="Non-interactive mode: process prompt and exit",
    )
    parser.add_argument(
        "--continue",
        "-c",
        action="store_true",
        dest="continue_",
        help="Continue the previous session",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a previous session",
    )
    parser.add_argument(
        "--session",
        metavar="PATH",
        help="Session file or ID to load",
    )
    parser.add_argument(
        "--fork",
        metavar="PATH",
        help="Fork from a session file",
    )
    parser.add_argument(
        "--session-dir",
        metavar="DIR",
        help="Session storage directory",
    )
    parser.add_argument(
        "--no-session",
        action="store_true",
        help="Disable session persistence",
    )
    parser.add_argument(
        "--models",
        metavar="PATTERNS",
        help="Comma-separated model patterns for scoped model selection",
    )
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="Disable all tools",
    )
    parser.add_argument(
        "--no-builtin-tools",
        action="store_true",
        help="Disable built-in tools",
    )
    parser.add_argument(
        "--tools",
        metavar="TOOLS",
        action="append",
        help="Tool paths to load",
    )
    parser.add_argument(
        "--thinking",
        metavar="LEVEL",
        choices=["off", "minimal", "low", "medium", "high", "xhigh"],
        help="Thinking/reasoning effort level",
    )
    parser.add_argument(
        "--extension",
        metavar="PATH",
        action="append",
        help="Path to extension",
    )
    parser.add_argument(
        "--no-extensions",
        action="store_true",
        help="Disable all extensions",
    )
    parser.add_argument(
        "--skill",
        metavar="PATH",
        action="append",
        help="Path to a skill file",
    )
    parser.add_argument(
        "--no-skills",
        action="store_true",
        help="Disable skills",
    )
    parser.add_argument(
        "--prompt-template",
        metavar="PATH",
        action="append",
        help="Path to a prompt template",
    )
    parser.add_argument(
        "--no-prompt-templates",
        action="store_true",
        help="Disable prompt templates",
    )
    parser.add_argument(
        "--theme",
        metavar="PATH",
        action="append",
        help="Path to a theme file",
    )
    parser.add_argument(
        "--no-themes",
        action="store_true",
        help="Disable custom themes",
    )
    parser.add_argument(
        "--no-context-files",
        action="store_true",
        help="Disable project context file discovery",
    )
    parser.add_argument(
        "--export",
        metavar="FILE",
        help="Export session file to HTML and exit",
    )

    parser.add_argument(
        "--list-models",
        nargs="?",
        const=True,
        metavar="SEARCH",
        help="List available models (with optional fuzzy search)",
    )
    parser.add_argument("--verbose", action="store_true", help="Force verbose startup")
    parser.add_argument(
        "--offline", action="store_true", help="Disable startup network operations"
    )
    parser.add_argument(
        "--temperature", type=float, metavar="TEMP", help="Model temperature"
    )
    parser.add_argument(
        "--max-tokens", type=int, metavar="N", help="Maximum tokens to generate"
    )
    parser.add_argument(
        "--version", "-v", action="store_true", help="Show version number"
    )

    parser.add_argument(
        "--session-action",
        metavar="ACTION",
        choices=["list", "show", "delete"],
        help="Session action (list, show, delete)",
    )
    parser.add_argument("--session-id", metavar="ID", help="Session ID or path")
    parser.add_argument(
        "--login-provider", metavar="PROVIDER", help="Provider ID to login"
    )
    parser.add_argument(
        "--list-provider", metavar="PROVIDER", help="Provider ID to list models for"
    )

    return parser


def parse_args(argv: list[str] | None = None) -> ParsedArgs:
    file_args: list[str] = []

    source = argv if argv is not None else sys.argv[1:]
    command_keywords = {"session", "config", "login", "list", "help"}
    command = None
    command_args: list[str] = []
    prompt_parts: list[str] = []

    # Detect subcommand by scanning for known keywords before --
    clean_parts: list[str] = []
    i = 0
    while i < len(source):
        arg = source[i]
        if arg == "--":
            clean_parts.extend(source[i:])
            prompt_parts.extend(source[i:])
            break
        if arg.startswith("@"):
            file_args.append(arg[1:])
            i += 1
            continue
        if arg.startswith("-"):
            clean_parts.append(arg)
            if arg in ("-v", "--version"):
                pass
            elif "=" in arg:
                pass
            else:
                if i + 1 < len(source) and not source[i + 1].startswith("-"):
                    i += 1
                    clean_parts.append(source[i])
            i += 1
            continue
        if arg in command_keywords:
            command = arg
            command_args = source[i + 1 :]
            break
        clean_parts.append(arg)
        prompt_parts.append(arg)
        i += 1
    else:
        command_args = []

    parser = build_parser()
    ns = parser.parse_args(clean_parts)

    def g(name: str, default: Any = None) -> Any:
        return getattr(ns, name, default)

    models_list: list[str] = []
    if g("models"):
        models_list = [m.strip() for m in g("models").split(",") if m.strip()]

    tools_list: list[str] = []
    if g("tools"):
        tools_list = [t.strip() for t in g("tools").split(",") if t.strip()]

    # Parse subcommand arguments
    session_action = None
    session_id = None
    login_provider = None
    list_provider = None
    if command == "session":
        parts = [a for a in command_args if not a.startswith("-")]
        if parts:
            session_action = parts[0]
            if len(parts) > 1:
                session_id = parts[1]
    elif command == "login":
        parts = [a for a in command_args if not a.startswith("-")]
        if parts:
            login_provider = parts[0]
    elif command == "list":
        parts = [a for a in command_args if not a.startswith("-")]
        if parts:
            list_provider = parts[0]

    return {
        "command": command,
        "prompt": " ".join(prompt_parts).strip(),
        "provider": g("provider"),
        "model": g("model"),
        "api_key": g("api_key"),
        "system_prompt": g("system_prompt"),
        "append_system_prompt": g("append_system_prompt") or [],
        "thinking": g("thinking"),
        "continue_": g("continue_", False),
        "resume": g("resume", False),
        "session": g("session"),
        "fork": g("fork"),
        "session_dir": g("session_dir"),
        "no_session": g("no_session", False),
        "models": models_list,
        "no_tools": g("no_tools", False),
        "no_builtin_tools": g("no_builtin_tools", False),
        "tools": tools_list,
        "extensions": g("extension") or [],
        "no_extensions": g("no_extensions", False),
        "skills": g("skill") or [],
        "no_skills": g("no_skills", False),
        "prompt_templates": g("prompt_template") or [],
        "no_prompt_templates": g("no_prompt_templates", False),
        "themes": g("theme") or [],
        "no_themes": g("no_themes", False),
        "no_context_files": g("no_context_files", False),
        "mode": g("mode", "text"),
        "print": g("print", False),
        "export": g("export"),
        "list_models": g("list_models"),
        "verbose": g("verbose", False),
        "offline": g("offline", False),
        "file_args": file_args,
        "temperature": g("temperature"),
        "max_tokens": g("max_tokens"),
        "session_action": session_action,
        "session_id": session_id,
        "login_provider": login_provider,
        "list_provider": list_provider,
    }
