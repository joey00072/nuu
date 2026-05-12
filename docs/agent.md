# Agent Runtime

`nuu.agent` is the reusable runtime for multi-turn LLM work with tool execution.
It depends on `nuu.ai` and does not import from `nuu.coding_agent` or `nuu.tui`.

Use this package when you need a stateful assistant loop, streaming events, tool
execution, steering messages, follow-up messages, or direct access to the
low-level async loop.

## Responsibility

The runtime owns four things:

- Conversation state: system prompt, messages, model, thinking level, tools, and
  current streaming status.
- Event lifecycle: user messages, assistant streaming updates, tool execution,
  turn boundaries, and run completion.
- Tool orchestration: argument validation, permission hooks, sequential or
  parallel execution, streamed tool updates, and tool-result messages.
- Queueing: steering messages that interrupt the next turn, and follow-up
  messages that run after current work is finished.

It does not know what a coding tool is, how sessions are persisted, how terminal
widgets render, or how CLI arguments are parsed.

## Key Files

| File | Purpose |
|---|---|
| `types.py` | `AgentTool`, `AgentToolResult`, `AgentContext`, `AgentLoopConfig`, event models, and hook context types |
| `agent_loop.py` | `run_agent_loop()` and `run_agent_loop_continue()`; stream assistant output, execute tools, and emit lifecycle events |
| `agent.py` | `Agent` facade; persistent state, queueing, subscriptions, abort, prompt, and continue behavior |
| `proxy.py` | Proxy and transport abstractions |

## Mental Model

```
Agent.prompt(...)
  -> normalize user input into AgentMessage values
  -> snapshot AgentContext
  -> build AgentLoopConfig
  -> run_agent_loop(...)
       transform_context()
       convert_to_llm()
       stream_simple() or stream_fn()
       emit assistant streaming events
       validate and execute tool calls
       emit toolResult messages
       repeat until no tools, no steering, no follow-ups
  -> Agent._process_events(...) updates state and notifies subscribers
```

`AgentContext.messages` is the source transcript passed between turns. The
provider layer receives only the messages returned by `convert_to_llm()`.

## Quick Start

```python
from __future__ import annotations

from nuu.agent.agent import Agent
from nuu.ai.models import get_model


model = get_model("anthropic", "claude-sonnet-4-20250514")
if model is None:
    raise RuntimeError("Model is not registered")

agent = Agent(
    initial_state={
        "system_prompt": "You are a concise assistant.",
        "model": model,
        "tools": [],
        "messages": [],
    },
)


def print_text(event) -> None:
    if event.type != "message_update":
        return
    assistant_event = event.assistant_message_event
    if assistant_event["type"] == "text_delta":
        print(assistant_event["delta"], end="")


agent.subscribe(print_text)
await agent.prompt("Explain this repository in one paragraph.")
```

## Message Conversion

The runtime works with `AgentMessage`, which is currently the same union as
`nuu.ai.types.Message`. Providers only receive standard LLM messages.

The default conversion keeps:

- `user`
- `assistant`
- `toolResult`

Custom applications can pass `convert_to_llm` to filter UI-only messages,
collapse history, or translate application-specific messages before each LLM
call.

```
AgentMessage[] -> transform_context() -> AgentMessage[] -> convert_to_llm() -> Message[] -> LLM
```

Use `transform_context` for pruning or compaction before conversion. Use
`convert_to_llm` for the final provider-facing transcript.

## Event Lifecycle

For a plain prompt with no tool calls:

```
agent_start
turn_start
message_start        user prompt
message_end          user prompt
message_start        assistant message
message_update       assistant streaming delta
message_update       assistant streaming delta
message_end          assistant final message
turn_end             assistant message, no tool results
agent_end
```

For a prompt with tool calls:

```
agent_start
turn_start
message_start/end    user prompt
message_start        assistant with tool call
message_update       assistant deltas
message_end          assistant final message
tool_execution_start
tool_execution_update  optional streamed progress
tool_execution_end
message_start/end    toolResult
turn_end
turn_start           follow-up LLM turn with toolResult in context
message_start        assistant response
message_update
message_end
turn_end
agent_end
```

`Agent.subscribe(listener)` listeners are called for every event. If a listener
returns an awaitable, the agent awaits it before continuing event processing.

## Event Types

| Event | Meaning |
|---|---|
| `agent_start` | A prompt or continue run has started |
| `agent_end` | The run has finished and contains the new messages from this run |
| `turn_start` | A new LLM turn is beginning |
| `turn_end` | The turn completed with an assistant message and any tool results |
| `message_start` | A user, assistant, or tool-result message began |
| `message_update` | Assistant streaming update; contains an `AssistantMessageEvent` |
| `message_end` | A message is complete and ready to persist |
| `tool_execution_start` | A tool call was accepted for preparation |
| `tool_execution_update` | A tool emitted a partial update |
| `tool_execution_end` | A tool call finalized successfully or as an error |

## AgentTool Contract

Tools implement the `AgentTool` protocol from `nuu.agent.types`:

```python
from __future__ import annotations

from collections.abc import Callable

from nuu.agent.types import AgentToolResult
from nuu.ai.types import TextContent


class ReadNoteTool:
    name = "read_note"
    description = "Read a named note."
    label = "Read Note"
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    execution_mode = "parallel"

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, object],
        on_update: Callable[[AgentToolResult[dict[str, str]]], None] | None = None,
    ) -> AgentToolResult[dict[str, str]]:
        name = str(params["name"])
        return AgentToolResult(
            content=[TextContent(type="text", text=f"note: {name}")],
            details={"name": name},
        )
```

Tool arguments are validated against `parameters` before `execute()` runs.
Raise an exception when execution fails. The loop converts exceptions into
`toolResult` messages with `is_error=True`.

`AgentToolResult.terminate=True` asks the loop to skip the automatic follow-up
LLM call after the current batch. It only takes effect when every finalized tool
result in the batch is terminating.

## Tool Execution

`AgentLoopConfig.tool_execution` controls batch behavior:

- `parallel`: default. Tool preflight runs in assistant source order, allowed
  tools execute concurrently, `tool_execution_end` emits as each tool finishes,
  and final `toolResult` messages are emitted in assistant source order.
- `sequential`: tools execute one by one in assistant source order.

A tool can set `execution_mode = "sequential"` to force the whole batch to run
sequentially.

## Tool Hooks

`before_tool_call(ctx)` runs after argument validation and before execution.
Return `BeforeToolCallResult(block=True, reason="...")` to deny execution and
emit an error tool result.

`after_tool_call(ctx)` runs after execution and before final tool events are
emitted. It can replace content, details, error status, or termination behavior.

Use these hooks for permission policy, audit data, and application-level
postprocessing. Keep tool-specific behavior inside the tool itself.

## Steering And Follow-Ups

Steering messages are delivered after the current assistant turn and its tool
calls finish. They run before follow-up messages.

Follow-up messages are delivered only when there are no remaining tool calls and
no steering messages.

```python
agent.steer(user_message)
agent.follow_up(user_message)
agent.clear_steering_queue()
agent.clear_follow_up_queue()
agent.clear_all_queues()
```

Queue modes are `one-at-a-time` or `all`:

- `one-at-a-time`: drain one queued message per opportunity.
- `all`: drain the whole queue at once.

## Continue And Abort

`await agent.continue_run()` resumes from the existing transcript without adding
a new user prompt. The last message must not be an assistant message unless
queued steering or follow-up messages can be drained into a new prompt.

`agent.abort()` cancels the active task. The facade records an assistant message
with `stop_reason="aborted"` and emits `agent_end`.

`await agent.wait_for_idle()` waits for the active task to settle.

## Low-Level Loop

Use `run_agent_loop()` when an application wants to own state updates itself:

```python
from __future__ import annotations

from nuu.agent.agent_loop import run_agent_loop
from nuu.agent.types import AgentContext, AgentLoopConfig, AgentEvent


async def emit(event: AgentEvent) -> None:
    print(event.type)


new_messages = await run_agent_loop(
    prompts=[user_message],
    context=AgentContext(system_prompt="...", messages=[], tools=[]),
    config=AgentLoopConfig(
        model=model,
        convert_to_llm=lambda messages: [
            message
            for message in messages
            if message.role in ("user", "assistant", "toolResult")
        ],
    ),
    emit=emit,
)
```

The low-level loop is useful for tests and alternate frontends. Prefer `Agent`
when subscribers must be awaited as a state barrier before tool preflight.

## State Surface

`Agent.state` returns the agent itself through the `AgentState` protocol:

- `system_prompt`
- `model`
- `thinking_level`
- `tools`
- `messages`
- `is_streaming`
- `streaming_message`
- `pending_tool_calls`
- `error_message`

Assigning `agent.tools` or `agent.messages` copies the top-level list before
storing it.

## Testing Guidance

Use `nuu.ai.providers.faux` for agent tests. Do not call real providers. Cover:

- Plain assistant response.
- Tool success.
- Tool exception converted to `is_error=True`.
- Blocked tool call through `before_tool_call`.
- Parallel execution ordering.
- `terminate=True` behavior.
- Abort and continue paths.
