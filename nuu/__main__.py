"""
Entry point for nuu. Launches the TUI when no prompt is given, or runs in
CLI mode when a prompt argument is provided.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="nuu - coding agent")
    parser.add_argument("prompt", nargs="?", help="Initial prompt (omit for TUI mode)")
    parser.add_argument("--model", help="Model ID")
    parser.add_argument("--provider", help="Provider ID")
    parser.add_argument("--tui", action="store_true", help="Force TUI mode")

    args = parser.parse_args()

    if not args.prompt or args.tui:
        asyncio.run(_run_tui(args))
    else:
        from nuu.coding_agent.main import main as cli_main

        cli_main(sys.argv[1:])


async def _run_tui(args: argparse.Namespace) -> None:
    from nuu.ai.providers import register_builtins  # noqa: F401
    from nuu.ai.models import get_model, get_models, get_providers
    from nuu.coding_agent.session import AgentSession
    from nuu.coding_agent.tools.index import create_all_tools
    from nuu.coding_agent.config import get_agent_dir
    from nuu.tui.app import run_tui

    cwd = os.getcwd()
    agent_dir = get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)

    providers = get_providers()
    if not providers:
        print("No models loaded. Check models.json.", file=sys.stderr)
        sys.exit(1)

    from nuu.coding_agent.session import _resolve_api_key

    # Try saved model from settings first
    saved_provider = None
    saved_model_id = None
    try:
        from nuu.coding_agent.config import get_settings_file
        from nuu.coding_agent.core.settings_manager import SettingsManager

        sm = SettingsManager(get_settings_file())
        saved_provider = sm.get("default_provider", "")
        saved_model_id = sm.get("default_model", "")
    except Exception:
        pass

    provider = args.provider
    if not provider and saved_provider and _resolve_api_key(saved_provider):
        provider = saved_provider
    if not provider:
        for p in providers:
            if _resolve_api_key(p):
                provider = p
                break
    if not provider:
        provider = providers[0]

    model = (
        get_model(provider, saved_model_id if not args.model else args.model)
        if provider
        else None
    )
    if not model:
        models = get_models(provider)
        if models:
            model = models[0]

    if not model:
        print(f"No model found for provider: {provider}", file=sys.stderr)
        sys.exit(1)

    session = AgentSession(
        cwd=cwd,
        model=model,
        tools=create_all_tools(cwd),
    )

    await run_tui(session)


if __name__ == "__main__":
    main()
