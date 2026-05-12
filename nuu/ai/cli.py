"""
CLI for OAuth provider login management (anthropic, github-copilot, openai-codex).
Supports interactive and non-interactive auth flows.

Owns: login(), list_providers(), auth file read/write.
Delegates to: argparse for CLI parsing, httpx for OAuth token exchange.

Data flow: CLI args -> login() -> OAuth flow -> auth.json

Depends on: nuu.ai only for module namespace; argparse, httpx, json, pathlib
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

AUTH_FILE = Path.home() / ".nuu" / "auth.json"

PROVIDERS: list[dict[str, str]] = [
    {"id": "anthropic", "name": "Anthropic"},
    {"id": "github-copilot", "name": "GitHub Copilot"},
    {"id": "openai-codex", "name": "OpenAI Codex"},
]


def _try_rich_prompt(message: str) -> str:
    try:
        from rich.prompt import Prompt

        return Prompt.ask(message)
    except ImportError:
        return input(f"{message} ")


def load_auth() -> dict[str, Any]:
    if not AUTH_FILE.exists():
        return {}
    try:
        return json.loads(AUTH_FILE.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_auth(data: dict[str, Any]) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(data, indent=2), "utf-8")


def login(provider: str | None = None) -> None:
    providers = PROVIDERS

    if provider is None:
        print("Select a provider:\n")
        for i, p in enumerate(providers, start=1):
            print(f"  {i}. {p['name']}")
        print()

        choice = _try_rich_prompt(f"Enter number (1-{len(providers)})")
        try:
            index = int(choice) - 1
        except ValueError:
            print("Invalid selection")
            sys.exit(1)

        if index < 0 or index >= len(providers):
            print("Invalid selection")
            sys.exit(1)

        provider = providers[index]["id"]

    if not any(p["id"] == provider for p in providers):
        print(f"Unknown provider: {provider}", file=sys.stderr)
        print("Use 'nuu ai list' to see available providers", file=sys.stderr)
        sys.exit(1)

    print(f"Logging in to {provider}...")

    oauth_providers = {
        "anthropic": _login_anthropic,
        "github-copilot": _login_github_copilot,
        "openai-codex": _login_openai_codex,
    }

    handler = oauth_providers.get(provider)
    if handler is None:
        print(f"No login handler for provider: {provider}", file=sys.stderr)
        sys.exit(1)

    handler(provider)


def _login_anthropic(provider_id: str) -> None:
    print(f"\nOpen the following URL in your browser to authorize {provider_id}:")
    print("  https://console.anthropic.com/oauth")
    print()
    code = _try_rich_prompt("Enter the authorization code:")
    if not code:
        print("No code provided, aborting.")
        sys.exit(1)
    auth = load_auth()
    auth[provider_id] = {"type": "oauth", "code": code}
    save_auth(auth)
    print(f"\nCredentials saved to {AUTH_FILE}")


def _login_github_copilot(provider_id: str) -> None:
    import httpx

    with httpx.Client() as client:
        resp = client.post(
            "https://github.com/login/device/code",
            data={
                "client_id": "Iv1.b99e6e6e6e6e6e6e",
                "scope": "read:user",
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        device_data = resp.json()
        verification_uri = device_data.get(
            "verification_uri", "https://github.com/login/device"
        )
        user_code = device_data.get("user_code", "")
        device_code = device_data.get("device_code", "")

        print("\nOpen the following URL in your browser:")
        print(f"  {verification_uri}")
        print(f"Enter code: {user_code}")
        print()
        input("Press Enter after completing authorization...")

        token_resp = client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": "Iv1.b99e6e6e6e6e6e6e",
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()

    auth = load_auth()
    auth[provider_id] = {
        "type": "oauth",
        "access": token_data.get("access_token", ""),
        "expires": 0,
    }
    save_auth(auth)
    print(f"\nCredentials saved to {AUTH_FILE}")


def _login_openai_codex(provider_id: str) -> None:
    print(f"\nOpen the following URL in your browser to authorize {provider_id}:")
    print("  https://chatgpt.com/api/auth")
    print()
    code = _try_rich_prompt("Enter the authorization code:")
    if not code:
        print("No code provided, aborting.")
        sys.exit(1)
    auth = load_auth()
    auth[provider_id] = {"type": "oauth", "code": code}
    save_auth(auth)
    print(f"\nCredentials saved to {AUTH_FILE}")


def list_providers() -> None:
    available = PROVIDERS
    print("Available OAuth providers:\n")
    for p in available:
        print(f"  {p['id']:<20} {p['name']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nuu ai",
        description="Nuu AI - OAuth provider management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command")

    login_parser = subparsers.add_parser("login", help="Login to an OAuth provider")
    login_parser.add_argument(
        "provider",
        nargs="?",
        metavar="PROVIDER",
        help="Provider ID to login to",
    )

    subparsers.add_parser("list", help="List available providers")

    subparsers.add_parser("help", help="Show this help message")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command or args.command == "help":
        parser.print_help()
        print()
        print("Providers:")
        for p in PROVIDERS:
            print(f"  {p['id']:<20} {p['name']}")
        print()
        print("Examples:")
        print("  nuu ai login                         Interactive provider selection")
        print("  nuu ai login anthropic                Login to specific provider")
        print("  nuu ai list                           List providers")
        return

    if args.command == "list":
        list_providers()
        return

    if args.command == "login":
        login(args.provider)
        return


if __name__ == "__main__":
    main()
