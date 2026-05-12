"""
CLI entry point for the coding agent. Parses arguments, resolves model,
creates session, reads stdin, builds initial message, and runs the agent
in print mode (text output to stdout).

Owns: main(), _main_async(), _resolve_model(), _run_print_mode().
Delegates to: nuu.coding_agent.cli.args for parsing, nuu.coding_agent.core.sdk
  for session creation, nuu.ai.stream for provider resolution.

Data flow: argv -> parse_args() -> register_builtin_providers() ->
  create_agent_session() -> session.prompt() -> stdout output

Depends on: nuu.agent.types, nuu.coding_agent.cli, nuu.coding_agent.core.sdk,
  nuu.ai, nuu.ai.providers.register_builtins
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from nuu.agent.types import (
    AgentEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionStartEvent,
)
from nuu.coding_agent.cli.args import ParsedArgs, parse_args
from nuu.coding_agent.cli.file_processor import read_stdin
from nuu.coding_agent.cli.initial_message import build_initial_message
from nuu.coding_agent.config import APP_NAME, VERSION, get_agent_dir
from nuu.coding_agent.core.sdk import (
    AgentSessionServices,
    create_agent_session_from_services,
    create_agent_session_services,
)
from nuu.coding_agent.session import AgentSession
from nuu.coding_agent.session_manager import SessionManager
from nuu.ai import ModelInfo, get_model, get_models, get_providers
from nuu.ai.providers.register_builtins import register_builtin_providers


def main(argv: list[str] | None = None) -> None:
    parsed = parse_args(argv)

    if parsed.get("version"):
        print(VERSION)
        return

    agent_dir = get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)

    _apply_offline_flag(parsed)

    asyncio.run(_main_async(parsed, agent_dir))


async def main_with_services(argv: list[str], services: AgentSessionServices) -> None:
    parsed = parse_args(argv)

    cwd = os.getcwd()
    model = _resolve_model(parsed, services)
    session = await _create_session(cwd, services, model, parsed)
    stdin_content = read_stdin()
    initial_message = _build_prompt(parsed, stdin_content)

    if initial_message:
        await _run_print_mode(session, initial_message)
    else:
        print(f"Usage: {APP_NAME} [OPTIONS] PROMPT", file=sys.stderr)
        print(f"Try '{APP_NAME} --help' for more information.", file=sys.stderr)


def _apply_offline_flag(parsed: ParsedArgs) -> None:
    is_offline = parsed.get("offline", False) or os.environ.get("NUU_OFFLINE") in (
        "1",
        "true",
        "yes",
    )
    if is_offline:
        os.environ["NUU_OFFLINE"] = "1"


async def _main_async(parsed: ParsedArgs, agent_dir: Path) -> None:
    register_builtin_providers()

    cwd = os.getcwd()
    services = create_agent_session_services(cwd=cwd, agent_dir=str(agent_dir))
    await services.resource_loader.reload()

    model = _resolve_model(parsed, services)
    session = await _create_session(cwd, services, model, parsed)

    stdin_content = read_stdin()
    initial_message = _build_prompt(parsed, stdin_content)

    if initial_message:
        await _run_print_mode(session, initial_message)
    else:
        print(f"Usage: {APP_NAME} [OPTIONS] PROMPT", file=sys.stderr)
        print(f"Try '{APP_NAME} --help' for more information.", file=sys.stderr)


def _resolve_model(parsed: ParsedArgs, services: AgentSessionServices) -> ModelInfo:
    provider = parsed.get("provider")
    model_pattern = parsed.get("model")

    if model_pattern and "/" in model_pattern:
        provider, model_id = model_pattern.split("/", 1)
    elif model_pattern:
        model_id = model_pattern
    else:
        settings = services.settings_manager
        provider = provider or settings.get("default_provider")
        model_id = settings.get("default_model")

    if provider and model_id:
        model = get_model(provider, model_id)
        if model:
            return model

    provider = provider or ""
    models = get_models(provider) if provider else []
    if models:
        return models[0]

    for p in get_providers():
        models = get_models(p)
        if models:
            return models[0]

    print("No models available. Configure a provider and model.", file=sys.stderr)
    sys.exit(1)


async def _create_session(
    cwd: str,
    services: AgentSessionServices,
    model: ModelInfo,
    parsed: ParsedArgs,
) -> AgentSession:
    session_manager = SessionManager.create(cwd)

    session = await create_agent_session_from_services(
        services=services,
        model=model,
        system_prompt=parsed.get("system_prompt", "") or "",
        cwd=cwd,
        session_manager=session_manager,
    )

    thinking = parsed.get("thinking")
    if thinking:
        session.agent.thinking_level = thinking

    api_key = parsed.get("api_key")
    if api_key:
        services.auth_storage.set_api_key(model.provider, api_key)

    return session


def _build_prompt(parsed: ParsedArgs, stdin_content: str | None) -> str | None:
    result = build_initial_message({"parsed": parsed, "stdin_content": stdin_content})
    return result.get("initial_message")


async def _run_print_mode(session: AgentSession, initial_message: str) -> int:
    def on_event(event: AgentEvent) -> None:
        if isinstance(event, MessageStartEvent) and event.message.role == "assistant":
            print("\nAssistant: ", end="", flush=True)
        elif isinstance(event, MessageUpdateEvent):
            evt = event.assistant_message_event
            if evt["type"] == "text_delta":
                print(evt["delta"], end="", flush=True)
        elif isinstance(event, MessageEndEvent):
            msg = event.message
            if msg.role == "toolResult":
                text_parts = [
                    c.text for c in msg.content if c.type == "text" and c.text
                ]
                if text_parts:
                    print(f"\n[Tool Result: {''.join(text_parts)[:200]}]", flush=True)
        elif isinstance(event, ToolExecutionStartEvent):
            print(f"\n[{event.tool_name}]", flush=True)

    session.subscribe(on_event)
    await session.prompt(initial_message)
    return 0
