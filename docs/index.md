# Nuu Documentation

Nuu is a Python implementation of Pi's terminal coding-agent architecture. It
keeps the provider layer, agent runtime, coding-agent behavior, and raw-terminal
UI separate so each part can be developed and tested without importing upward.

## Start Here

- [Coding agent](coding_agent.md) - CLI, sessions, tools, slash commands, settings,
  and the interactive application wiring.
- [Agent runtime](agent.md) - stateful agent facade, event lifecycle, tool
  execution, steering, follow-ups, and the low-level loop.
- [AI provider layer](ai.md) - provider registration, streaming events, model
  metadata, credentials, and test providers.
- [Terminal UI](tui.md) - raw terminal engine, input routing, rendering,
  keybindings, overlays, and TUI invariants.

## Quick Start

```bash
uv sync
uv run nuu
```

Authenticate by setting the provider API key in the environment, or run `/login`
inside the TUI for providers that have guidance in
`nuu/coding_agent/core/auth_guidance.py`.

## Development Commands

| Task | Command |
|---|---|
| Run the CLI | `uv run nuu` |
| Run as module | `uv run python -m nuu` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Type check | `uv run mypy` |
| Test | `uv run pytest` |

After code changes, run `uv run ruff check .` and `uv run mypy`. If tests were
added or modified, run the relevant test file too.

## Architecture Map

```
nuu.ai
  Provider adapters, model metadata, credentials, and normalized assistant
  message events.

nuu.agent
  Stateful agent facade and low-level loop. Consumes nuu.ai streams and executes
  tools. Does not know about files, bash, sessions, or terminal UI.

nuu.tui
  Raw ANSI terminal engine and widgets. Does not know about provider APIs or
  coding tools.

nuu.coding_agent
  Product layer. Creates coding tools, assembles prompts, manages sessions and
  settings, and wires nuu.agent to nuu.tui.
```

Lower modules never import upward. Keep new behavior in the lowest module that
can own it cleanly.

## Common Workflows

| Goal | Start With |
|---|---|
| Add a provider | `docs/ai.md`, then `nuu/ai/providers/register_builtins.py` |
| Add a coding tool | `docs/coding_agent.md`, then `nuu/coding_agent/tools/` |
| Change event behavior | `docs/agent.md`, then `nuu/agent/agent_loop.py` |
| Change keyboard behavior | `docs/tui.md`, then `nuu/tui/engine/keybindings.py` |
| Debug sessions | `docs/coding_agent.md`, then `nuu/coding_agent/session_manager.py` |

## Non-Negotiable Invariants

- Provider code emits normalized `AssistantMessageEvent` values. Upper layers do
  not inspect raw provider wire formats.
- Agent tools implement `AgentTool` and return `AgentToolResult`; failed tools
  raise exceptions or are converted into `is_error` tool results by the loop.
- Application key checks go through `TUI_KEYBINDINGS` and
  `kb.matches(data, "binding.name")`.
- Kitty key release events are filtered before editor input handling.
- Session files are JSONL and append typed entries under `~/.nuu/sessions/`.
