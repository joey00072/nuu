# Coding Agent

`nuu.coding_agent` is the product layer. It turns the reusable `nuu.agent`
runtime into a terminal coding assistant with tools, sessions, prompts, slash
commands, settings, model selection, and a TUI.

It depends on `nuu.agent`, `nuu.ai`, and `nuu.tui`. Nothing below it imports from
`nuu.coding_agent`.

## Quick Start

```bash
uv sync
uv run nuu
```

Run a one-shot prompt:

```bash
uv run nuu --print "summarize this repository"
```

Continue or resume sessions:

```bash
uv run nuu --continue
uv run nuu --resume
uv run nuu --session ~/.nuu/sessions/path/to/session.jsonl
```

Disable persistence for a scratch run:

```bash
uv run nuu --no-session
```

## Responsibility

The coding agent owns:

- CLI parsing and startup mode selection.
- Model/provider selection and authentication guidance.
- System prompt assembly.
- Built-in file and shell tools.
- Session persistence and replay.
- Slash command definitions and dispatch.
- Settings storage and migrations.
- Wiring `Agent` events into `NuuApp`.

It does not implement provider wire formats, the core agent loop, or raw terminal
rendering.

## Runtime Flow

```
nuu.__main__
  -> coding_agent.cli.args.parse_args()
  -> coding_agent.main
       load settings
       resolve provider/model/auth
       create tools
       build CodingAgentSession
       create NuuApp
       subscribe TUI to Agent events
       run prompt, print mode, JSON/RPC mode, or interactive TUI
```

## Key Files

| File | Purpose |
|---|---|
| `main.py` | Entry point; wires settings, session, agent, and TUI |
| `session.py` | `CodingAgentSession`; in-memory app session and agent wrapper |
| `session_manager.py` | JSONL persistence, replay, continue, resume, fork, and import/export helpers |
| `cli/args.py` | `argparse` parser, output modes, session flags, provider/model flags, env var docs |
| `tools/index.py` | `create_all_tools(cwd)`; central built-in tool registry |
| `tools/*.py` | Built-in `AgentTool` implementations |
| `core/system_prompt.py` | System prompt assembly |
| `core/settings_manager.py` | Typed settings read/write |
| `core/model_resolver.py` | Default model selection |
| `core/provider_display_names.py` | Human-readable provider names |
| `core/auth_guidance.py` | `/login` instructions and provider auth text |
| `slash_commands.py` | Built-in slash command names and descriptions |
| `skills.py` | Skill and prompt-template loading |
| `compaction.py` | Context compaction behavior |
| `migrations.py` | Session and settings migrations |

## Built-In Tools

`create_all_tools(cwd)` currently registers:

| Tool | Purpose |
|---|---|
| `ls` | List directory contents |
| `read` | Read file contents |
| `write` | Write a file |
| `bash` | Run shell commands |
| `edit` | Edit an existing file |
| `find` | Find files by path/name |
| `grep` | Search file contents |

All tools receive the session `cwd` at construction. Tools should resolve paths
relative to that working directory and use `pathlib.Path` internally.

## Adding A Tool

1. Create `nuu/coding_agent/tools/<name>.py`.
2. Implement the `AgentTool` protocol from `nuu.agent.types`.
3. Use Pydantic models for structured boundary data.
4. Add the tool instance to `create_all_tools()` in `tools/index.py`.
5. Add tests under `tests/coding_agent/tools/`.

Tool failures should raise specific exceptions when meaningful. The agent loop
turns raised exceptions into `toolResult` messages with `is_error=True`.

Permission policy belongs in `AgentLoopConfig.before_tool_call`, not inside tool
execution, unless the tool itself cannot safely operate without a local guard.

## Slash Commands

Slash command metadata lives in `slash_commands.py`:

```python
BuiltinSlashCommand(name="model", description="Select model (opens selector UI)")
```

Handlers live in `nuu/tui/app.py` through `NuuApp._dispatch_slash()`. The slash
picker reads `BUILTIN_SLASH_COMMANDS` for display.

Built-in commands include:

| Command | Purpose |
|---|---|
| `/settings` | Open settings menu |
| `/model` | Select model |
| `/scoped-models` | Enable or disable models for cycling |
| `/login`, `/logout` | Configure or remove provider authentication |
| `/new` | Start a new session |
| `/resume` | Resume a different session |
| `/session` | Show session metadata and stats |
| `/tree` | Navigate the session tree |
| `/fork` | Fork from a previous user message |
| `/clone` | Duplicate current session state |
| `/compact` | Manually compact context |
| `/export`, `/import`, `/share` | Move or publish session data |
| `/copy` | Copy the last agent message |
| `/hotkeys` | Show keyboard shortcuts |
| `/reload` | Reload configured resources |
| `/debug` | Print debug information |
| `/quit` | Quit |

When adding a command, update both the metadata and dispatch path.

## Sessions

Sessions are JSONL files under `~/.nuu/sessions/` unless overridden by
`NUU_SESSION_DIR` or `--session-dir`.

Line 1 is a header:

```json
{"type":"session","version":3,"id":"...","timestamp":"...","cwd":"...","parentSession":null}
```

Later lines are typed entries with `type`, `id`, `parentId`, and `timestamp`.

| Entry | Payload |
|---|---|
| `SessionMessageEntry` | Full `AgentMessage` |
| `ThinkingLevelChangeEntry` | `thinkingLevel` |
| `ModelChangeEntry` | `provider`, `modelId` |
| `CompactionEntry` | `summary`, `firstKeptEntryId`, `tokensBefore` |

`SessionManager.build_session_context()` replays entries to reconstruct the
active `AgentContext` on resume.

## Settings

Settings are read through `SettingsManager`, not by opening
`~/.nuu/settings.json` directly from feature code.

Use migrations in `migrations.py` for schema changes. Keep settings typed and
validate at the boundary.

## Models And Auth

Provider and model selection flows through:

- `core/model_resolver.py` for default model behavior.
- `core/provider_display_names.py` for UI labels.
- `core/auth_guidance.py` for `/login` setup instructions.
- `nuu.ai.env_api_keys` for environment-variable credential lookup.

`cli/args.py` documents provider-related environment variables, including
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`,
Amazon Bedrock credentials, Azure OpenAI settings, Cloudflare credentials, and
`NUU_OFFLINE`.

## Output Modes

`--mode` accepts:

| Mode | Purpose |
|---|---|
| `text` | Human-readable output |
| `json` | Structured JSON event output |
| `rpc` | JSONL RPC integration |

`--print` runs non-interactively and exits after the prompt.

## Context And Customization Flags

The CLI has flags for loading or disabling tools, extensions, skills, prompt
templates, themes, and context files:

```bash
uv run nuu --no-tools
uv run nuu --no-builtin-tools
uv run nuu --skill path/to/SKILL.md
uv run nuu --prompt-template path/to/template.md
uv run nuu --theme path/to/theme.json
uv run nuu --no-context-files
```

Only document features that are wired in this repo. If a flag is parsed but a
feature is incomplete, document the implemented behavior and track the missing
work outside user-facing docs.

## TUI Integration

`NuuApp` subscribes to `Agent` events and renders:

- user messages
- assistant streaming content
- thinking blocks
- tool execution status and output
- system/error messages
- editor, footer, and overlays

Input flows through the raw terminal engine:

```
ProcessTerminal -> StdinBuffer -> TUI._handle_input -> NuuApp._handle_input -> overlay or Editor
```

Application key checks must use `TUI_KEYBINDINGS` through
`kb.matches(data, "binding.name")`.

## Testing

After code changes:

```bash
uv run ruff check .
uv run mypy
```

If tests were added or changed:

```bash
uv run pytest tests/path/to/test_file.py
```

Use the faux provider for LLM tests. Do not use real provider credentials in
tests.

For TUI behavior, use a tmux session:

```bash
tmux new-session -d -s nuu-test -x 220 -y 50
tmux send-keys -t nuu-test "cd /Users/joey/workspace/nuu && uv run nuu" Enter
sleep 2 && tmux capture-pane -t nuu-test -p
tmux kill-session -t nuu-test
```
