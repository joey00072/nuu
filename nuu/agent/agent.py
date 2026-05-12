"""
Stateful Agent facade wrapping the agent loop. Manages message history,
tool execution queues, steering/follow-up message queues, and lifecycle
events. Implements the AgentState protocol.

Owns: Agent class, PendingMessageQueue, state management.
Delegates to: run_agent_loop / run_agent_loop_continue for LLM interaction.

Data flow: external prompt() call -> Agent._run_with_lifecycle() ->
  run_agent_loop() -> Agent._process_events() for state updates

Depends on: nuu.ai.types, nuu.agent.agent_loop, nuu.agent.types
"""

import asyncio
import inspect
import time
from typing import Any, Callable, Literal, Coroutine

from ..ai.types import (
    AssistantMessage,
    ImageContent,
    Message,
    ModelInfo,
    ProviderResponse,
    TextContent,
    ThinkingBudgets,
    Transport,
    Usage,
    UsageCost,
)
from .agent_loop import run_agent_loop, run_agent_loop_continue
from .types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentTool,
    BeforeToolCallContext,
    BeforeToolCallResult,
    StreamFn,
    TurnEndEvent,
    AgentEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    MessageEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionEndEvent,
    ThinkingLevel,
    AgentState,
)

EMPTY_USAGE = Usage(
    input=0,
    output=0,
    cache_read=0,
    cache_write=0,
    total_tokens=0,
    cost=UsageCost(input=0, output=0, cache_read=0, cache_write=0, total=0),
)

DEFAULT_MODEL = ModelInfo(
    id="unknown",
    name="unknown",
    api="unknown",
    provider="unknown",
    base_url="",
    reasoning=False,
    input=[],
    cost={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
    context_window=0,
    max_tokens=0,
)


def default_convert_to_llm(messages: list[AgentMessage]) -> list[Message]:
    return [m for m in messages if m.role in ("user", "assistant", "toolResult")]


class PendingMessageQueue:
    def __init__(self, mode: Literal["all", "one-at-a-time"] = "all") -> None:
        self.mode = mode
        self._messages: list[AgentMessage] = []

    def enqueue(self, message: AgentMessage) -> None:
        self._messages.append(message)

    def has_items(self) -> bool:
        return len(self._messages) > 0

    def drain(self) -> list[AgentMessage]:
        if self.mode == "all":
            drained = self._messages.copy()
            self._messages.clear()
            return drained
        if not self._messages:
            return []
        first = self._messages.pop(0)
        return [first]

    def clear(self) -> None:
        self._messages.clear()


class Agent:
    def __init__(
        self,
        initial_state: dict[str, Any] | None = None,
        *,
        convert_to_llm: Callable[
            [list[AgentMessage]], list[Message] | Coroutine[Any, Any, list[Message]]
        ]
        | None = None,
        transform_context: Callable[
            [list[AgentMessage]],
            list[AgentMessage] | Coroutine[Any, Any, list[AgentMessage]],
        ]
        | None = None,
        get_api_key: Callable[[str], str | Coroutine[Any, Any, str | None] | None]
        | None = None,
        stream_fn: StreamFn | None = None,
        before_tool_call: (
            Callable[
                [BeforeToolCallContext],
                BeforeToolCallResult
                | Coroutine[Any, Any, BeforeToolCallResult | None]
                | None,
            ]
            | None
        ) = None,
        after_tool_call: (
            Callable[
                [AfterToolCallContext],
                AfterToolCallResult
                | Coroutine[Any, Any, AfterToolCallResult | None]
                | None,
            ]
            | None
        ) = None,
        on_payload: Callable[[Any, ModelInfo], Any] | None = None,
        on_response: Callable[[ProviderResponse, ModelInfo], None] | None = None,
        steering_mode: Literal["all", "one-at-a-time"] = "one-at-a-time",
        follow_up_mode: Literal["all", "one-at-a-time"] = "one-at-a-time",
        session_id: str | None = None,
        thinking_budgets: ThinkingBudgets | None = None,
        transport: Transport = "auto",
        max_retry_delay_ms: int | None = None,
        tool_execution: Literal["sequential", "parallel"] = "parallel",
    ):
        state = initial_state or {}
        self.system_prompt = state.get("system_prompt", "")
        self.model = state.get("model", DEFAULT_MODEL)
        self.thinking_level: ThinkingLevel = state.get("thinking_level", "off")
        self._tools: list[AgentTool[Any]] = state.get("tools", [])
        self._messages: list[AgentMessage] = state.get("messages", [])

        self.is_streaming = False
        self.streaming_message: AgentMessage | None = None
        self.pending_tool_calls: set[str] = set()
        self.error_message: str | None = None

        self.convert_to_llm = convert_to_llm or default_convert_to_llm
        self.transform_context = transform_context
        self.get_api_key = get_api_key
        self.stream_fn = stream_fn
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.on_payload = on_payload
        self.on_response = on_response
        self.session_id = session_id
        self.thinking_budgets = thinking_budgets
        self.transport = transport
        self.max_retry_delay_ms = max_retry_delay_ms
        self.tool_execution = tool_execution

        self.steering_queue = PendingMessageQueue(mode=steering_mode)
        self.follow_up_queue = PendingMessageQueue(mode=follow_up_mode)

        self._listeners: list[Callable[[AgentEvent], Any]] = []
        self._active_task: asyncio.Task | None = None
        self._abort_event: asyncio.Event | None = None

    @property
    def tools(self) -> list[AgentTool[Any]]:
        return self._tools

    @tools.setter
    def tools(self, value: list[AgentTool[Any]]) -> None:
        self._tools = list(value)

    @property
    def messages(self) -> list[AgentMessage]:
        return self._messages

    @messages.setter
    def messages(self, value: list[AgentMessage]) -> None:
        self._messages = list(value)

    @property
    def steering_mode(self) -> Literal["all", "one-at-a-time"]:
        return self.steering_queue.mode

    @steering_mode.setter
    def steering_mode(self, mode: Literal["all", "one-at-a-time"]) -> None:
        self.steering_queue.mode = mode

    @property
    def follow_up_mode(self) -> Literal["all", "one-at-a-time"]:
        return self.follow_up_queue.mode

    @follow_up_mode.setter
    def follow_up_mode(self, mode: Literal["all", "one-at-a-time"]) -> None:
        self.follow_up_queue.mode = mode

    @property
    def signal(self) -> asyncio.Event | None:
        return self._abort_event

    def subscribe(self, listener: Callable[[AgentEvent], Any]):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @property
    def state(self) -> AgentState:
        return self  # Matches AgentState Protocol

    def steer(self, message: AgentMessage):
        self.steering_queue.enqueue(message)

    def follow_up(self, message: AgentMessage):
        self.follow_up_queue.enqueue(message)

    def clear_steering_queue(self):
        self.steering_queue.clear()

    def clear_follow_up_queue(self):
        self.follow_up_queue.clear()

    def clear_all_queues(self):
        self.clear_steering_queue()
        self.clear_follow_up_queue()

    def has_queued_messages(self) -> bool:
        return self.steering_queue.has_items() or self.follow_up_queue.has_items()

    def abort(self):
        if self._abort_event:
            self._abort_event.set()
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()

    async def wait_for_idle(self):
        if self._active_task:
            try:
                await self._active_task
            except asyncio.CancelledError:
                pass

    def reset(self):
        self.messages = []
        self.is_streaming = False
        self.streaming_message = None
        self.pending_tool_calls = set()
        self.error_message = None
        self.clear_all_queues()

    async def prompt(
        self,
        message: str | AgentMessage | list[AgentMessage],
        images: list[ImageContent] | None = None,
    ):
        if self._active_task and not self._active_task.done():
            raise RuntimeError("Agent is already processing")

        prompts = self._normalize_prompt_input(message, images)
        self._active_task = asyncio.create_task(
            self._run_with_lifecycle(
                lambda: run_agent_loop(
                    prompts,
                    self._create_context_snapshot(),
                    self._create_loop_config(),
                    self._process_events,
                    self._abort_event,
                    self.stream_fn,
                )
            )
        )
        await self._active_task

    async def continue_run(self):
        if self._active_task and not self._active_task.done():
            raise RuntimeError("Agent is already processing")

        if not self.messages:
            raise RuntimeError("No messages to continue from")

        last_message = self.messages[-1]
        if getattr(last_message, "role", None) == "assistant":
            queued_steering = self.steering_queue.drain()
            if queued_steering:
                await self.prompt(queued_steering)
                return

            queued_follow_ups = self.follow_up_queue.drain()
            if queued_follow_ups:
                await self.prompt(queued_follow_ups)
                return

            raise RuntimeError("Cannot continue from assistant message")

        self._active_task = asyncio.create_task(
            self._run_with_lifecycle(
                lambda: run_agent_loop_continue(
                    self._create_context_snapshot(),
                    self._create_loop_config(),
                    self._process_events,
                    self._abort_event,
                    self.stream_fn,
                )
            )
        )
        await self._active_task

    def _normalize_prompt_input(
        self,
        input_val: str | AgentMessage | list[AgentMessage],
        images: list[ImageContent] | None = None,
    ) -> list[AgentMessage]:
        if isinstance(input_val, list):
            return input_val
        if not isinstance(input_val, str):
            return [input_val]

        content = [TextContent(type="text", text=input_val)]
        if images:
            content.extend(images)
        from ..ai.types import UserMessage

        return [
            UserMessage(role="user", content=content, timestamp=int(time.time() * 1000))
        ]

    async def _run_with_lifecycle(
        self, executor: Callable[[], Coroutine[Any, Any, list[AgentMessage]]]
    ):
        self._abort_event = asyncio.Event()
        self.is_streaming = True
        self.streaming_message = None
        self.error_message = None

        try:
            await executor()
        except asyncio.CancelledError:
            await self._handle_failure(RuntimeError("Aborted"), aborted=True)
        except Exception as e:
            await self._handle_failure(e)
        finally:
            self.is_streaming = False
            self.streaming_message = None
            self.pending_tool_calls.clear()
            self._abort_event = None

    async def _handle_failure(self, error: Exception, aborted: bool = False):
        msg = AssistantMessage(
            role="assistant",
            content=[TextContent(type="text", text="")],
            api=self.model.api,
            provider=self.model.provider,
            model=self.model.id,
            usage=EMPTY_USAGE,
            stop_reason="aborted" if aborted else "error",
            error_message=str(error),
            timestamp=int(time.time() * 1000),
        )
        self.messages.append(msg)
        self.error_message = msg.error_message
        await self._process_events(AgentEndEvent(messages=[msg]))

    def _create_context_snapshot(self) -> AgentContext:
        return AgentContext(
            system_prompt=self.system_prompt,
            messages=self.messages.copy(),
            tools=self.tools.copy(),
        )

    def _create_loop_config(self) -> AgentLoopConfig:
        return AgentLoopConfig(
            model=self.model,
            reasoning=None if self.thinking_level == "off" else self.thinking_level,
            session_id=self.session_id,
            thinking_budgets=self.thinking_budgets,
            transport=self.transport,
            max_retry_delay_ms=self.max_retry_delay_ms,
            tool_execution=self.tool_execution,
            convert_to_llm=self.convert_to_llm,
            transform_context=self.transform_context,
            get_api_key=self.get_api_key,
            get_steering_messages=lambda: self.steering_queue.drain(),
            get_follow_up_messages=lambda: self.follow_up_queue.drain(),
            before_tool_call=self.before_tool_call,
            after_tool_call=self.after_tool_call,
            on_payload=self.on_payload,
            on_response=self.on_response,
        )

    async def _process_events(self, event: AgentEvent):
        if isinstance(event, MessageStartEvent):
            self.streaming_message = event.message
        elif isinstance(event, MessageUpdateEvent):
            self.streaming_message = event.message
        elif isinstance(event, MessageEndEvent):
            self.streaming_message = None
            self.messages.append(event.message)
        elif isinstance(event, ToolExecutionStartEvent):
            self.pending_tool_calls.add(event.tool_call_id)
        elif isinstance(event, ToolExecutionEndEvent):
            self.pending_tool_calls.discard(event.tool_call_id)
        elif isinstance(event, TurnEndEvent):
            if getattr(event.message, "role", None) == "assistant" and getattr(
                event.message, "error_message", None
            ):
                self.error_message = event.message.error_message
        elif isinstance(event, AgentEndEvent):
            self.streaming_message = None

        for listener in self._listeners:
            res = listener(event)
            if inspect.isawaitable(res):
                await res
