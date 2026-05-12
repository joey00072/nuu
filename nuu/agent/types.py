"""
Agent-level type definitions: AgentTool protocol, AgentEvent types,
AgentLoopConfig, AgentContext, and lifecycle event models. Interfaces
between the AI streaming layer and the agent loop layer.

Owns: AgentTool protocol, AgentEvent TypedDicts, AgentContext, AgentLoopConfig.
Delegates to: nuu.ai.types for AssistantMessage, ModelInfo, etc.

Data flow: AgentLoopConfig -> agent_loop -> AgentEvent stream

Depends on: nuu.ai.types (AssistantMessage, Message, ModelInfo, etc.)
"""

from __future__ import annotations

from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Generic,
    Literal,
    Protocol,
    TypeVar,
    Union,
    runtime_checkable,
)

from ..ai.event_stream import AssistantMessageEventStream
from ..ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    ImageContent,
    Message,
    ModelInfo,
    PiBaseModel,
    ProviderResponse,
    SimpleStreamOptions,
    TextContent,
    ToolCall,
    ToolResultMessage,
)

TDetails = TypeVar("TDetails")

# Stream function contract used by the agent loop.
StreamFn = Callable[
    [ModelInfo, Context, SimpleStreamOptions | None],
    AssistantMessageEventStream | Awaitable[AssistantMessageEventStream],
]

# Configuration for how tool calls from a single assistant message are executed.
ToolExecutionMode = Literal["sequential", "parallel"]

# A single tool call content block emitted by an assistant message.
AgentToolCall = ToolCall


class AgentToolResult(PiBaseModel, Generic[TDetails]):
    content: list[Union[TextContent, ImageContent]]
    details: TDetails
    terminate: bool | None = None


AgentToolUpdateCallback = Callable[[AgentToolResult[TDetails]], None]


@runtime_checkable
class AgentTool(Protocol, Generic[TDetails]):
    name: str
    description: str
    parameters: dict[str, Any]
    label: str
    execution_mode: ToolExecutionMode | None = None

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        on_update: AgentToolUpdateCallback[TDetails] | None = None,
    ) -> AgentToolResult[TDetails]: ...


# Extensible via type merging/subclassing in Python
AgentMessage = Message


class AgentContext(PiBaseModel):
    system_prompt: str
    messages: list[AgentMessage]
    tools: list[AgentTool[Any]] | None = None


class BeforeToolCallResult(PiBaseModel):
    block: bool | None = None
    reason: str | None = None


class AfterToolCallResult(PiBaseModel):
    content: list[Union[TextContent, ImageContent]] | None = None
    details: Any | None = None
    is_error: bool | None = None
    terminate: bool | None = None


class BeforeToolCallContext(PiBaseModel):
    assistant_message: AssistantMessage
    tool_call: AgentToolCall
    args: Any
    context: AgentContext


class AfterToolCallContext(PiBaseModel):
    assistant_message: AssistantMessage
    tool_call: AgentToolCall
    args: Any
    result: AgentToolResult[Any]
    is_error: bool
    context: AgentContext


class ShouldStopAfterTurnContext(PiBaseModel):
    message: AssistantMessage
    tool_results: list[ToolResultMessage]
    context: AgentContext
    new_messages: list[AgentMessage]


class AgentLoopConfig(SimpleStreamOptions):
    model: ModelInfo
    convert_to_llm: Callable[
        [list[AgentMessage]], list[Message] | Coroutine[Any, Any, list[Message]]
    ]
    transform_context: (
        Callable[
            [list[AgentMessage]],
            list[AgentMessage] | Coroutine[Any, Any, list[AgentMessage]],
        ]
        | None
    ) = None
    get_api_key: (
        Callable[[str], str | Coroutine[Any, Any, str | None] | None] | None
    ) = None
    should_stop_after_turn: (
        Callable[[ShouldStopAfterTurnContext], bool | Coroutine[Any, Any, bool]]
        | None
    ) = None
    get_steering_messages: (
        Callable[[], list[AgentMessage] | Coroutine[Any, Any, list[AgentMessage]]]
        | None
    ) = None
    get_follow_up_messages: (
        Callable[[], list[AgentMessage] | Coroutine[Any, Any, list[AgentMessage]]]
        | None
    ) = None
    tool_execution: ToolExecutionMode = "parallel"
    before_tool_call: (
        Callable[
            [BeforeToolCallContext],
            BeforeToolCallResult
            | Coroutine[Any, Any, BeforeToolCallResult | None]
            | None,
        ]
        | None
    ) = None
    after_tool_call: (
        Callable[
            [AfterToolCallContext],
            AfterToolCallResult
            | Coroutine[Any, Any, AfterToolCallResult | None]
            | None,
        ]
        | None
    ) = None
    on_payload: Callable[[Any, ModelInfo], Any] | None = None
    on_response: Callable[[ProviderResponse, ModelInfo], None] | None = None


ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]


@runtime_checkable
class AgentState(Protocol):
    system_prompt: str
    model: ModelInfo
    thinking_level: ThinkingLevel
    tools: list[AgentTool[Any]]
    messages: list[AgentMessage]
    is_streaming: bool
    streaming_message: AgentMessage | None
    pending_tool_calls: set[str]
    error_message: str | None


# Agent Events
class AgentStartEvent(PiBaseModel):
    type: Literal["agent_start"] = "agent_start"


class AgentEndEvent(PiBaseModel):
    type: Literal["agent_end"] = "agent_end"
    messages: list[AgentMessage]


class TurnStartEvent(PiBaseModel):
    type: Literal["turn_start"] = "turn_start"


class TurnEndEvent(PiBaseModel):
    type: Literal["turn_end"] = "turn_end"
    message: AgentMessage
    tool_results: list[ToolResultMessage]


class MessageStartEvent(PiBaseModel):
    type: Literal["message_start"] = "message_start"
    message: AgentMessage


class MessageUpdateEvent(PiBaseModel):
    type: Literal["message_update"] = "message_update"
    message: AgentMessage
    assistant_message_event: AssistantMessageEvent


class MessageEndEvent(PiBaseModel):
    type: Literal["message_end"] = "message_end"
    message: AgentMessage


class ToolExecutionStartEvent(PiBaseModel):
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call_id: str
    tool_name: str
    args: Any


class ToolExecutionUpdateEvent(PiBaseModel):
    type: Literal["tool_execution_update"] = "tool_execution_update"
    tool_call_id: str
    tool_name: str
    args: Any
    partial_result: Any


class ToolExecutionEndEvent(PiBaseModel):
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call_id: str
    tool_name: str
    result: Any
    is_error: bool


AgentEvent = Union[
    AgentStartEvent,
    AgentEndEvent,
    TurnStartEvent,
    TurnEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    MessageEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolExecutionEndEvent,
]
