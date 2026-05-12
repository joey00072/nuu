# Development Rules

## Conversational Style

- Keep answers short and concise
- No emojis in code, commits, docs, or PRs
- No fluff or cheerful filler text
- Technical prose only; be kind but direct

## Code Quality

- Read files in full before making wide-ranging changes, before editing files you have not fully inspected, and when the user asks you to investigate or audit something. Do not rely only on search snippets.
- Modern Python 3.11+ type hints everywhere
- Pydantic (`BaseModel`) for all data models at boundaries; use `ConfigDict(alias_generator=to_camel, populate_by_name=True)` for camelCase JSON interop with Pi protocol
- `httpx` for HTTP, `argparse` for CLI
- `snake_case` for functions, variables, file names
- `from __future__ import annotations` at the top of every `.py` file
- No comments in code — write self-documenting code with clear names
- Use `X | None`, never `Optional[X]`
- Use `list[X]`, `dict[K, V]`, never `List[X]`, `Dict[K, V]`
- Standard top-level imports only; no inline/conditional imports unless breaking an actual import cycle at a documented boundary
- Early returns and guard clauses over deeply nested `if`/`else` blocks
- `pathlib.Path` for filesystem paths
- Catch specific exceptions only when there is meaningful recovery; chain with `raise ... from exc`
- No `Any` types unless unavoidable at an external boundary; narrow immediately
- Keep functions small and single-purpose
- Use f-strings for string formatting; never `.format()` or `%`
- Async generators (`async for`, `async yield`) for streaming data; `asyncio.create_task` for concurrent work
- `TypedDict` for event types constructed as dict literals; `BaseModel` for everything else
- **NEVER hardcode key checks** like `data == "\x1b[A"` directly in application code. All keybindings must be registered in `nuu/tui/engine/keybindings.py` and matched via `kb.matches(data, "binding.name")`.
- Always ask before removing functionality that appears intentional
- Do not preserve backward compatibility unless the user explicitly asks

## Commands

| Action | Command |
|---|---|
| Sync environment | `uv sync` |
| Run module | `uv run python -m nuu` |
| Run CLI | `uv run nuu` |
| Lint | `uv run ruff check .` |
| Auto-fix lint | `uv run ruff check . --fix` |
| Format | `uv run ruff format .` |
| Type check | `uv run mypy` |
| Test (all) | `uv run pytest` |
| Test (verbose) | `uv run pytest -vv` |
| Test (stop on first failure) | `uv run pytest -x` |
| Test one file | `uv run pytest tests/path/to/test_file.py` |
| Test one test | `uv run pytest tests/path/to/test_file.py::test_name` |
| Test by keyword | `uv run pytest -k "keyword"` |
| Add dependency | `uv add <package>` |
| Remove dependency | `uv remove <package>` |

After code changes (not documentation-only): run `uv run ruff check .` and `uv run mypy`. Fix all errors before committing. If you create or modify a test file, run it and iterate until it passes.

NEVER commit unless the user asks.

## Architecture

Five separable modules mirroring Pi's package layout (`ref/pi/packages/`). Lower modules never import upward.

### `nuu/ai/` — LLM Provider Layer

Provider adapters, model metadata, streaming event types, token usage, credentials, and provider capability normalization.

Key files:
- `types.py` — shared Pydantic models (`AgentMessage`, `MessageContent`) and `AssistantMessageEvent` TypedDicts
- `api_registry.py` — provider registration and lookup
- `models.py` — model metadata; do NOT edit `models.py` directly to add individual models; instead update provider fetch logic
- `stream.py` — `stream()` entry point that dispatches to registered providers
- `env_api_keys.py` — env var → API key mapping
- `providers/` — one file per provider: `anthropic.py`, `openai_responses.py`, `openai_completions.py`, `openai_codex_responses.py`, `google.py`, `google_vertex.py`, `amazon_bedrock.py`, `mistral.py`, `azure_openai_responses.py`, `cloudflare.py`, `faux.py` (test provider)
- `providers/transform_messages.py` — shared message conversion utilities
- `providers/register_builtins.py` — calls `register_api_provider()` for all built-in providers

### `nuu/agent/` — Agent Runtime

General agent loop, stateful agent facade, typed message/event types, tool execution contracts, and proxy abstractions.

Key files:
- `types.py` — `AgentTool` protocol, `AgentToolResult`, `AgentMessage`, event types. Contains JS-style comments (pre-existing from porting); do not remove them.
- `agent.py` — stateful `Agent` facade
- `agent_loop.py` — core async agent loop with tool execution
- `proxy.py` — proxy/transport abstractions

### `nuu/coding_agent/` — Coding Agent Behavior

Coding agent tools, session manager, prompt templates, skills, slash commands, settings, migrations, and CLI. Depends on `nuu.agent`, `nuu.ai`, and `nuu.tui`.

Key files:
- `main.py` — entry point, wires agent + TUI
- `session.py` / `session_manager.py` — session persistence (JSONL)
- `tools/index.py` — `create_all_tools()`, add new tools here
- `tools/` — `bash.py`, `read.py`, `write.py`, `edit.py`, `find.py`, `grep.py`, `ls.py`
- `core/settings_manager.py` — settings read/write
- `core/model_resolver.py` — default model per provider
- `core/provider_display_names.py` — human-readable provider names for UI
- `core/auth_guidance.py` — `/login` setup instructions per provider
- `core/system_prompt.py` — system prompt assembly
- `cli/args.py` — CLI argument parsing and env var documentation
- `slash_commands.py` — built-in slash command definitions

### `nuu/tui/` — Terminal UI

Custom raw-terminal rendering engine ported from Pi's `ref/pi/packages/tui/src/`. **Does not use Textual.** All rendering is done via direct ANSI escape sequences with differential updates.

#### `nuu/tui/engine/` — Core Engine (port of Pi's `packages/tui/src/`)

- `tui.py` — `TUI` class: differential rendering, overlay management, input routing, scrollback. Use `tui.set_bottom(component)` for the pinned bottom area and `tui.emit(lines)` to append to scrollback.
- `terminal.py` — `ProcessTerminal`: raw mode, Kitty keyboard protocol, modifyOtherKeys fallback, bracketed paste, stdin buffering via `StdinBuffer`
- `editor.py` — `Editor`: multi-line editor with history, cursor movement, keybinding-driven input
- `keys.py` — key parsing: `parse_kitty_sequence()`, `decode_kitty_printable()`, `matches_key()`, `is_key_release()`, `is_key_repeat()`
- `keybindings.py` — `KeybindingsManager` + `TUI_KEYBINDINGS` dict; all application keybindings are defined here
- `ansi.py` — ANSI helpers: `visible_width()`, `strip_ansi()`, `fg()`, `pad_to_width()`, `slice_by_column()`
- `component.py` — base `Component`, `Container`, `Text`, `Box`, `Spacer`
- `markdown.py` — `MarkdownComponent` renderer
- `theme.py` — color theme

#### `nuu/tui/widgets/` — App Components

- `messages.py` — `AssistantMessageComponent` (streaming text + thinking), `UserMessageComponent`, `ToolExecutionComponent`, `SystemMessageComponent`, `ErrorMessageComponent`, `SpinnerComponent`
- `footer.py` — `FooterComponent`
- `slash_picker.py` — `SlashPickerComponent`
- `model_picker.py` — `ModelPickerComponent`
- `login_picker.py` — `LoginPickerComponent`, `AuthTypeComponent`
- `api_key_input.py` — `ApiKeyInputComponent`
- `chat_input.py`, `status_bar.py` — supplementary widgets

Note: `nuu/tui/widgets/spinner.py` is stale (references Textual); the active spinner is `SpinnerComponent` in `messages.py`.

#### `nuu/tui/app.py` — Application

`NuuApp` wires all components. `_BottomComponent` owns the pinned bottom area (streaming content, then spinner below it, then editor, then footer). Input flows: `ProcessTerminal` → `StdinBuffer` → `TUI._handle_input` → `NuuApp._handle_input` → overlay or `Editor`.

### Key TUI Behaviors

These must be preserved when modifying the TUI:

**Kitty keyboard protocol** (`terminal.py`): On start, the terminal queries Kitty support (`\x1b[?u`). If confirmed, Kitty protocol is enabled (`\x1b[>7u`), which reports press, repeat, and release events. **Key release events MUST be filtered** — `app._handle_input` drops them via `is_key_release()`, and `decode_kitty_printable()` returns `None` for release events (event type 3). Without this, every keypress inserts characters twice.

**Escape** (`app._on_escape`): Escape closes the active overlay if any, aborts the agent if busy, and **does nothing when idle**. Escape never quits the application.

**Ctrl+C** (`app._on_interrupt`): When busy, aborts the agent. When idle, the first press clears the editor; a second press within 500 ms quits. This matches Pi's `app.clear` double-press behavior.

**Bottom render order** (`_BottomComponent.render`): streaming content first, spinner second. The spinner appears below thinking/response text, not above it.

## Testing nuu TUI with tmux

To test the TUI in a controlled terminal environment:

```bash
# Create tmux session with specific dimensions
tmux new-session -d -s nuu-test -x 220 -y 50

# Start nuu
tmux send-keys -t nuu-test "cd /Users/joey/workspace/nuu && uv run nuu" Enter

# Wait for startup, then capture output
sleep 2 && tmux capture-pane -t nuu-test -p

# Send input
tmux send-keys -t nuu-test "your prompt here" Enter

# Send special keys
tmux send-keys -t nuu-test Escape
tmux send-keys -t nuu-test C-c

# Cleanup
tmux kill-session -t nuu-test
```

## Adding a New LLM Provider

1. **`nuu/ai/types.py`** — Add API identifier to `KnownApi` enum and provider name to `KnownProvider` enum.

2. **`nuu/ai/providers/<name>.py`** — Create provider file exporting:
   - `stream_<name>()` returning `AsyncIterator[AssistantMessageEvent]` — emits standardized events: `text_start`, `text_delta`, `thinking_start`, `thinking_delta`, `toolcall_start`, `toolcall_delta`, `usage`, `stop`
   - `stream_simple_<name>()` for `SimpleStreamOptions` mapping
   - Message/tool conversion helpers

3. **`nuu/ai/providers/register_builtins.py`** — Register via `register_api_provider(ApiProvider(...))`.

4. **`nuu/ai/env_api_keys.py`** — Add env var mapping in `_API_KEY_ENV_MAP`. For non-standard auth (OAuth, ADC), handle in `get_env_api_key()` — see `google-vertex` and `amazon-bedrock` cases.

5. **`nuu/coding_agent/core/auth_guidance.py`** — Add auth guidance string so `/login` shows correct setup instructions.

6. **`nuu/coding_agent/core/provider_display_names.py`** — Add display name to `BUILT_IN_PROVIDER_DISPLAY_NAMES`.

7. **`nuu/coding_agent/core/model_resolver.py`** — Add default model ID for the provider.

8. **`nuu/coding_agent/cli/args.py`** — Add env var documentation.

9. **Tests** — Add provider to `tests/ai/` with at least one stream test using the faux provider pattern. No real API keys.

## Adding New Tools

1. Create `nuu/coding_agent/tools/<name>.py`
2. Add an instance to `create_all_tools()` in `nuu/coding_agent/tools/index.py`
3. Implement the `AgentTool` protocol (`nuu/agent/types.py`):
   - Fields: `name: str`, `description: str`, `parameters: dict` (JSON schema), `label: str`
   - Method: `async execute(tool_call_id, params, on_update) -> AgentToolResult`
   - Use a Pydantic model for structured result data (`DetailsType`)
4. Add tests covering success, failure, and edge cases

## Adding TUI Keybindings

1. Add the binding definition to `TUI_KEYBINDINGS` in `nuu/tui/engine/keybindings.py`
2. Use `kb.matches(data, "tui.your.binding")` everywhere the binding is checked
3. Never match raw escape sequences directly in application code

## Session File Format

Sessions stored as JSONL in `~/.nuu/sessions/` via `SessionManager`:

- **Line 1**: `SessionHeader` — `{"type": "session", "version": 3, "id": "...", "timestamp": "...", "cwd": "...", "parentSession": null}`
- **Subsequent lines**: typed entries with `type`, `id`, `parentId`, `timestamp`:
  - `SessionMessageEntry` — `message` field with full `AgentMessage`
  - `ThinkingLevelChangeEntry` — `thinkingLevel` field
  - `ModelChangeEntry` — `provider` and `modelId` fields
  - `CompactionEntry` — `summary`, `firstKeptEntryId`, `tokensBefore`

## Testing

- `pytest` with `asyncio_mode = auto` (configured in `pyproject.toml`)
- Tests mirror the package layout under `tests/`
- Use `nuu/ai/providers/faux.py` for all LLM tests — no real API keys, no network, no paid tokens
- Put shared fixtures in `tests/conftest.py`
- Add regression tests under `tests/` with descriptive names for bugs
- Cover negative paths: permissions, malformed provider events, failed tool calls, interrupted streams, session persistence errors
- Use `tmp_path` fixture for filesystem-related tests
- If you create or modify a test, run it and iterate until it passes

## Git Rules for Parallel Agents

Multiple agents may work in the same worktree simultaneously. Follow these rules strictly:

- **Never use `git add -A` or `git add .`** — these sweep up changes from other agents
- Stage only specific files: `git add path/to/file.py`
- Before committing, run `git status` and verify only YOUR files are staged
- Never run destructive git commands: `git reset --hard`, `git checkout .`, `git clean -fd`, `git stash`
- Never force push
- If rebase conflicts occur in files you did not modify, abort and ask for guidance
- Never commit unless the user explicitly asks

### Safe Workflow

```bash
# 1. Check status first
git status

# 2. Add ONLY your specific files
git add nuu/ai/providers/my_provider.py
git add nuu/ai/types.py

# 3. Commit
git commit -m "feat(ai): add my-provider support"
```
