# nuu/ai

LLM provider layer. No imports from upper packages (`agent`, `coding_agent`, `tui`).

## Responsibility

Knows how to talk to LLM APIs. Translates provider-specific wire formats into a
uniform event stream. Everything above this layer works exclusively with
`AssistantMessageEvent` and `AssistantMessage` — never with raw HTTP responses.

## Key Files

| File | Purpose |
|---|---|
| `types.py` | All shared types: `KnownApi`, `KnownProvider`, `ModelInfo`, `Message` variants, `AssistantMessageEvent` TypedDicts, `StreamOptions` |
| `api_registry.py` | Registry mapping API identifiers to `ApiProvider` instances |
| `stream.py` | Public entry points: `stream()`, `complete()`, `stream_simple()`, `complete_simple()` |
| `event_stream.py` | `EventStream[T,R]` async queue; `AssistantMessageEventStream` |
| `models.py` | Model metadata; do not hand-edit to add models |
| `env_api_keys.py` | Env var → API key mapping; non-standard auth (ADC, OAuth) |
| `validation.py` | `validate_tool_arguments()` — JSON schema validation for tool calls |
| `providers/register_builtins.py` | Registers all built-in providers at import time |
| `providers/faux.py` | Deterministic test provider — no network, no keys |
| `providers/transform_messages.py` | Shared message conversion across providers |

## Data Flow

```
ModelInfo + Context + Options
  -> stream() / stream_simple()       [stream.py]
  -> get_api_provider(model.api)      [api_registry.py]
  -> ApiProvider.stream()             [providers/<name>.py]
  -> AssistantMessageEventStream      [event_stream.py]
     push(event) per chunk ...
     push(stop event) -> result()
  -> AssistantMessage
```

## Event Stream

`EventStream` is a generic async queue (`asyncio.Queue`) with a typed result.
Providers push events via `stream.push(event)`. A `stop` event resolves
`stream.result()`. Callers iterate with `async for event in stream`.

Providers must always emit `stop` or call `stream.end()` on error — otherwise
callers block forever.

## AssistantMessageEvent Types

TypedDicts discriminated by `type`:

- `text_start`, `text_delta` — streaming text
- `thinking_start`, `thinking_delta` — extended thinking
- `toolcall_start`, `toolcall_delta` — tool call accumulation
- `usage` — token/cost accounting
- `stop` — terminal event

## Provider Registration

Each provider file exports `stream_<name>()` and `stream_simple_<name>()`.
`register_builtins.py` calls `register_api_provider(ApiProvider(...))` for each.
`stream.py` dispatches via `get_api_provider(model.api)`.

## Test Provider

`providers/faux.py` — use in all tests. Configure responses via `FauxStreamOptions`.
API: `"faux"`, provider: `"faux"`, model: `"faux-1"`. Never use real API keys in tests.
