"""
Public API surface for the agent module. Re-exports Agent, agent loop functions,
event types, tool protocol, and the AgentOptions TypedDict.

Owns: the canonical import path for agent-level constructs.
Delegates to: each submodule for implementation.

Depends on: nuu.agent.agent, nuu.agent.agent_loop, nuu.agent.types,
  nuu.ai.types (Message, ThinkingBudgets, Transport)
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine, Literal, TypedDict

from ..ai.types import Message, ModelInfo, ProviderResponse, ThinkingBudgets, Transport

from .agent import Agent, default_convert_to_llm
from .agent_loop import (
    agent_loop,
    agent_loop_continue,
    run_agent_loop,
    run_agent_loop_continue,
)
from .types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentStartEvent,
    AgentState,
    AgentTool,
    AgentToolCall,
    AgentToolResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ShouldStopAfterTurnContext,
    StreamFn,
    ThinkingLevel,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolExecutionMode,
    TurnEndEvent,
    TurnStartEvent,
)

try:
    from .proxy import ProxyStreamOptions, stream_proxy
except ImportError:
    pass


class AgentOptions(TypedDict, total=False):
    initial_state: dict[str, Any] | None
    convert_to_llm: Callable[[list[AgentMessage]], list[Message] | Coroutine[Any, Any, list[Message]]] | None
    transform_context: Callable[[list[AgentMessage]], list[AgentMessage] | Coroutine[Any, Any, list[AgentMessage]]] | None
    get_api_key: Callable[[str], str | Coroutine[Any, Any, str | None] | None] | None
    stream_fn: StreamFn | None
    before_tool_call: Callable[..., Any] | None
    after_tool_call: Callable[..., Any] | None
    on_payload: Callable[[Any, ModelInfo], Any] | None
    on_response: Callable[[ProviderResponse, ModelInfo], None] | None
    steering_mode: Literal["all", "one-at-a-time"]
    follow_up_mode: Literal["all", "one-at-a-time"]
    session_id: str | None
    thinking_budgets: ThinkingBudgets | None
    transport: Transport
    max_retry_delay_ms: int | None
    tool_execution: Literal["sequential", "parallel"]


__all__ = [
    "Agent",
    "AgentOptions",
    "default_convert_to_llm",
    "agent_loop",
    "agent_loop_continue",
    "run_agent_loop",
    "run_agent_loop_continue",
    "stream_proxy",
    "ProxyStreamOptions",
    "AfterToolCallContext",
    "AfterToolCallResult",
    "AgentContext",
    "AgentEndEvent",
    "AgentEvent",
    "AgentLoopConfig",
    "AgentMessage",
    "AgentStartEvent",
    "AgentState",
    "AgentTool",
    "AgentToolCall",
    "AgentToolResult",
    "BeforeToolCallContext",
    "BeforeToolCallResult",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "ShouldStopAfterTurnContext",
    "StreamFn",
    "ThinkingLevel",
    "ToolExecutionEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "ToolExecutionMode",
    "TurnEndEvent",
    "TurnStartEvent",
]
