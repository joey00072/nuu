"""
Core agent loop: orchestrates LLM streaming, tool call execution, steering/
follow-up processing, and turn management. Implements the message/turn/agent
event lifecycle.

Owns: run_agent_loop(), run_agent_loop_continue(), tool execution
  (sequential and parallel), event emission.
Delegates to: stream_simple() for LLM responses, validate_tool_call for
  argument validation, AgentTool.execute() for individual tool runs.

Data flow: AgentContext + AgentLoopConfig -> LLM stream -> tool executions ->
  loop until stop condition -> list[AgentMessage]

Depends on: nuu.ai.stream, nuu.ai.types, nuu.ai.validation, nuu.ai.event_stream,
  nuu.agent.types
"""

import asyncio
import time
from typing import Any, Callable, Coroutine, TypeVar

from ..ai.event_stream import EventStream
from ..ai.stream import stream_simple
from ..ai.types import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    TextContent,
    Context as AiContext,
)
from ..ai.validation import validate_tool_arguments
from .types import (
    AfterToolCallContext,
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentStartEvent,
    AgentTool,
    AgentToolResult,
    BeforeToolCallContext,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ShouldStopAfterTurnContext,
    StreamFn,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)

AgentEventSink = Callable[[AgentEvent], Coroutine[Any, Any, None]]
T = TypeVar("T")


async def ensure_awaitable(res: T | Coroutine[Any, Any, T]) -> T:
    if asyncio.iscoroutine(res):
        return await res
    return res


def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    signal: asyncio.Event | None = None,
    stream_fn: StreamFn | None = None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    stream = create_agent_stream()

    async def _emit(e):
        stream.push(e)

    async def _run():
        messages = await run_agent_loop(
            prompts, context, config, _emit, signal, stream_fn
        )
        stream.end(messages)

    asyncio.create_task(_run())
    return stream


def agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: asyncio.Event | None = None,
    stream_fn: StreamFn | None = None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")
    if getattr(context.messages[-1], "role", None) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")

    stream = create_agent_stream()

    async def _emit(e):
        stream.push(e)

    async def _run():
        messages = await run_agent_loop_continue(
            context, config, _emit, signal, stream_fn
        )
        stream.end(messages)

    asyncio.create_task(_run())
    return stream


def create_agent_stream() -> EventStream[AgentEvent, list[AgentMessage]]:
    return EventStream[AgentEvent, list[AgentMessage]](
        is_complete=lambda e: isinstance(e, AgentEndEvent),
        extract_result=lambda e: e.messages if isinstance(e, AgentEndEvent) else [],
    )


async def run_agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: asyncio.Event | None = None,
    stream_fn: StreamFn | None = None,
) -> list[AgentMessage]:
    new_messages: list[AgentMessage] = prompts.copy()
    current_context = AgentContext(
        system_prompt=context.system_prompt,
        messages=context.messages + prompts,
        tools=context.tools,
    )

    await emit(AgentStartEvent())
    await emit(TurnStartEvent())
    for prompt in prompts:
        await emit(MessageStartEvent(message=prompt))
        await emit(MessageEndEvent(message=prompt))

    await _run_loop(current_context, new_messages, config, emit, signal, stream_fn)
    return new_messages


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: asyncio.Event | None = None,
    stream_fn: StreamFn | None = None,
) -> list[AgentMessage]:
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")
    if getattr(context.messages[-1], "role", None) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")

    new_messages: list[AgentMessage] = []
    current_context = AgentContext(
        system_prompt=context.system_prompt,
        messages=context.messages.copy(),
        tools=context.tools,
    )

    await emit(AgentStartEvent())
    await emit(TurnStartEvent())

    await _run_loop(current_context, new_messages, config, emit, signal, stream_fn)
    return new_messages


async def _run_loop(
    current_context: AgentContext,
    new_messages: list[AgentMessage],
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: asyncio.Event | None,
    stream_fn: StreamFn | None,
) -> None:
    first_turn = True
    pending_messages: list[AgentMessage] = (
        await ensure_awaitable(config.get_steering_messages())
        if config.get_steering_messages
        else []
    )

    while True:
        has_more_tool_calls = True

        while has_more_tool_calls or pending_messages:
            if not first_turn:
                await emit(TurnStartEvent())
            else:
                first_turn = False

            if pending_messages:
                for message in pending_messages:
                    await emit(MessageStartEvent(message=message))
                    await emit(MessageEndEvent(message=message))
                    current_context.messages.append(message)
                    new_messages.append(message)
                pending_messages = []

            message = await _stream_assistant_response(
                current_context, config, emit, signal, stream_fn
            )
            new_messages.append(message)

            if message.stop_reason in ("error", "aborted"):
                await emit(TurnEndEvent(message=message, tool_results=[]))
                await emit(AgentEndEvent(messages=new_messages))
                return

            tool_calls = [c for c in message.content if c.type == "toolCall"]
            tool_results: list[ToolResultMessage] = []
            has_more_tool_calls = False

            if tool_calls:
                batch = await _execute_tool_calls(
                    current_context, message, config, emit
                )
                tool_results.extend(batch["messages"])
                has_more_tool_calls = not batch["terminate"]

                for result in tool_results:
                    current_context.messages.append(result)
                    new_messages.append(result)

            await emit(TurnEndEvent(message=message, tool_results=tool_results))

            if config.should_stop_after_turn:
                should_stop = await ensure_awaitable(
                    config.should_stop_after_turn(
                        ShouldStopAfterTurnContext(
                            message=message,
                            tool_results=tool_results,
                            context=current_context,
                            new_messages=new_messages,
                        )
                    )
                )
                if should_stop:
                    await emit(AgentEndEvent(messages=new_messages))
                    return

            pending_messages = (
                await ensure_awaitable(config.get_steering_messages())
                if config.get_steering_messages
                else []
            )

        follow_up = (
            await ensure_awaitable(config.get_follow_up_messages())
            if config.get_follow_up_messages
            else []
        )
        if follow_up:
            pending_messages = follow_up
            continue

        break

    await emit(AgentEndEvent(messages=new_messages))


async def _stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: asyncio.Event | None,
    stream_fn: StreamFn | None,
) -> AssistantMessage:
    messages = context.messages
    if config.transform_context:
        messages = await ensure_awaitable(config.transform_context(messages))

    llm_messages = await ensure_awaitable(config.convert_to_llm(messages))

    ai_context = AiContext(
        system_prompt=context.system_prompt,
        messages=llm_messages,
        tools=[
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in (context.tools or [])
        ],
    )

    api_key = (
        await ensure_awaitable(config.get_api_key(config.model.provider))
        if config.get_api_key
        else config.api_key
    )

    _stream = stream_fn if stream_fn is not None else stream_simple
    response = _stream(
        config.model,
        ai_context,
        config.model_copy(update={"api_key": api_key}),
    )
    response = await ensure_awaitable(response)

    partial_message: AssistantMessage | None = None
    added_partial = False

    if signal is not None and signal.is_set():
        raise asyncio.CancelledError("Aborted")

    async for event in response:
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError("Aborted")
        if event["type"] == "start":
            partial_message = event["partial"]
            context.messages.append(partial_message)
            added_partial = True
            await emit(MessageStartEvent(message=partial_message))
        elif event["type"] in (
            "text_start",
            "text_delta",
            "text_end",
            "thinking_start",
            "thinking_delta",
            "thinking_end",
            "toolcall_start",
            "toolcall_delta",
            "toolcall_end",
        ):
            if partial_message:
                partial_message = event["partial"]
                context.messages[-1] = partial_message
                await emit(
                    MessageUpdateEvent(
                        message=partial_message, assistant_message_event=event
                    ),
                )
        elif event["type"] in ("done", "error"):
            final_message = await response.result()
            if added_partial:
                context.messages[-1] = final_message
            else:
                context.messages.append(final_message)
                await emit(MessageStartEvent(message=final_message))
            await emit(MessageEndEvent(message=final_message))
            return final_message

    final_message = await response.result()
    if added_partial:
        context.messages[-1] = final_message
    else:
        context.messages.append(final_message)
        await emit(MessageStartEvent(message=final_message))
    await emit(MessageEndEvent(message=final_message))
    return final_message


async def _execute_tool_calls(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    emit: AgentEventSink,
) -> dict[str, Any]:
    tool_calls = [c for c in assistant_message.content if c.type == "toolCall"]
    has_sequential = any(
        next((t for t in (current_context.tools or []) if t.name == tc.name), None)
        and next(
            (t for t in (current_context.tools or []) if t.name == tc.name)
        ).execution_mode
        == "sequential"
        for tc in tool_calls
    )

    if config.tool_execution == "sequential" or has_sequential:
        return await _execute_tool_calls_sequential(
            current_context, assistant_message, tool_calls, config, emit
        )
    return await _execute_tool_calls_parallel(
        current_context, assistant_message, tool_calls, config, emit
    )


async def _execute_tool_calls_sequential(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[ToolCall],
    config: AgentLoopConfig,
    emit: AgentEventSink,
) -> dict[str, Any]:
    finalized_calls = []
    messages = []

    for tool_call in tool_calls:
        await emit(
            ToolExecutionStartEvent(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                args=tool_call.arguments,
            )
        )

        preparation = await _prepare_tool_call(
            current_context, assistant_message, tool_call, config
        )

        if preparation["kind"] == "immediate":
            finalized = {
                "tool_call": tool_call,
                "result": preparation["result"],
                "is_error": preparation["is_error"],
            }
        else:
            executed = await _execute_prepared_tool_call(preparation, emit)
            finalized = await _finalize_executed_tool_call(
                current_context, assistant_message, preparation, executed, config
            )

        await _emit_tool_execution_end(finalized, emit)
        res_msg = _create_tool_result_message(finalized)
        await _emit_tool_result_message(res_msg, emit)

        finalized_calls.append(finalized)
        messages.append(res_msg)

    terminate = len(finalized_calls) > 0 and all(
        f["result"].terminate for f in finalized_calls
    )
    return {"messages": messages, "terminate": terminate}


async def _execute_tool_calls_parallel(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[ToolCall],
    config: AgentLoopConfig,
    emit: AgentEventSink,
) -> dict[str, Any]:
    finalized_tasks: list[asyncio.Future[Any]] = []

    for tool_call in tool_calls:
        await emit(
            ToolExecutionStartEvent(
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                args=tool_call.arguments,
            )
        )

        preparation = await _prepare_tool_call(
            current_context, assistant_message, tool_call, config
        )
        if preparation["kind"] == "immediate":
            finalized = {
                "tool_call": tool_call,
                "result": preparation["result"],
                "is_error": preparation["is_error"],
            }
            await _emit_tool_execution_end(finalized, emit)
            finalized_tasks.append(asyncio.Future())
            finalized_tasks[-1].set_result(finalized)
            continue

        async def run_task(p=preparation):
            executed = await _execute_prepared_tool_call(p, emit)
            finalized = await _finalize_executed_tool_call(
                current_context, assistant_message, p, executed, config
            )
            await _emit_tool_execution_end(finalized, emit)
            return finalized

        finalized_tasks.append(asyncio.create_task(run_task()))

    ordered_finalized = await asyncio.gather(*finalized_tasks)
    messages = []
    for finalized in ordered_finalized:
        res_msg = _create_tool_result_message(finalized)
        await _emit_tool_result_message(res_msg, emit)
        messages.append(res_msg)

    terminate = len(ordered_finalized) > 0 and all(
        f["result"].terminate for f in ordered_finalized
    )
    return {"messages": messages, "terminate": terminate}


async def _prepare_tool_call(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_call: ToolCall,
    config: AgentLoopConfig,
) -> dict[str, Any]:
    tool = next(
        (t for t in (current_context.tools or []) if t.name == tool_call.name), None
    )
    if not tool:
        return {
            "kind": "immediate",
            "result": _create_error_tool_result(f"Tool {tool_call.name} not found"),
            "is_error": True,
        }

    try:
        prepare_args = getattr(tool, "prepare_arguments", None)
        if prepare_args:
            prepared_arguments = prepare_args(tool_call.arguments)
            if (
                prepared_arguments is not None
                and prepared_arguments is not tool_call.arguments
            ):
                tool_call = tool_call.model_copy(
                    update={"arguments": prepared_arguments}
                )

        from ..ai.types import Tool as AiTool

        ai_tool = AiTool(
            name=tool.name, description=tool.description, parameters=tool.parameters
        )
        validated_args = validate_tool_arguments(ai_tool, tool_call)

        if config.before_tool_call:
            before = await ensure_awaitable(
                config.before_tool_call(
                    BeforeToolCallContext(
                        assistant_message=assistant_message,
                        tool_call=tool_call,
                        args=validated_args,
                        context=current_context,
                    )
                )
            )
            if before and before.block:
                return {
                    "kind": "immediate",
                    "result": _create_error_tool_result(
                        before.reason or "Tool execution was blocked"
                    ),
                    "is_error": True,
                }
        return {
            "kind": "prepared",
            "tool_call": tool_call,
            "tool": tool,
            "args": validated_args,
        }
    except Exception as e:
        return {
            "kind": "immediate",
            "result": _create_error_tool_result(str(e)),
            "is_error": True,
        }


async def _execute_prepared_tool_call(
    prepared: dict[str, Any], emit: AgentEventSink
) -> dict[str, Any]:
    tool: AgentTool = prepared["tool"]
    tool_call: ToolCall = prepared["tool_call"]
    args = prepared["args"]

    update_futures: list[asyncio.Task[None]] = []

    def on_update(partial_result: AgentToolResult):
        fut = asyncio.ensure_future(
            emit(
                ToolExecutionUpdateEvent(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    args=tool_call.arguments,
                    partial_result=partial_result,
                )
            )
        )
        update_futures.append(fut)

    try:
        result = await tool.execute(
            tool_call_id=tool_call.id, params=args, on_update=on_update
        )
        return {"result": result, "is_error": False}
    except Exception as e:
        return {
            "result": _create_error_tool_result(str(e)),
            "is_error": True,
        }
    finally:
        if update_futures:
            await asyncio.gather(*update_futures)


async def _finalize_executed_tool_call(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    prepared: dict[str, Any],
    executed: dict[str, Any],
    config: AgentLoopConfig,
) -> dict[str, Any]:
    result = executed["result"]
    is_error = executed["is_error"]

    if config.after_tool_call:
        try:
            after = await ensure_awaitable(
                config.after_tool_call(
                    AfterToolCallContext(
                        assistant_message=assistant_message,
                        tool_call=prepared["tool_call"],
                        args=prepared["args"],
                        result=result,
                        is_error=is_error,
                        context=current_context,
                    )
                )
            )
            if after:
                result = AgentToolResult(
                    content=after.content
                    if after.content is not None
                    else result.content,
                    details=after.details
                    if after.details is not None
                    else result.details,
                    terminate=after.terminate
                    if after.terminate is not None
                    else result.terminate,
                )
                is_error = after.is_error if after.is_error is not None else is_error
        except Exception as e:
            result = _create_error_tool_result(str(e))
            is_error = True

    return {"tool_call": prepared["tool_call"], "result": result, "is_error": is_error}


def _create_error_tool_result(message: str) -> AgentToolResult[Any]:
    return AgentToolResult(content=[TextContent(type="text", text=message)], details={})


async def _emit_tool_execution_end(finalized: dict[str, Any], emit: AgentEventSink):
    await emit(
        ToolExecutionEndEvent(
            tool_call_id=finalized["tool_call"].id,
            tool_name=finalized["tool_call"].name,
            result=finalized["result"],
            is_error=finalized["is_error"],
        )
    )


def _create_tool_result_message(finalized: dict[str, Any]) -> ToolResultMessage:
    return ToolResultMessage(
        role="toolResult",
        tool_call_id=finalized["tool_call"].id,
        tool_name=finalized["tool_call"].name,
        content=finalized["result"].content,
        details=finalized["result"].details,
        is_error=finalized["is_error"],
        timestamp=int(time.time() * 1000),
    )


async def _emit_tool_result_message(res_msg: ToolResultMessage, emit: AgentEventSink):
    await emit(MessageStartEvent(message=res_msg))
    await emit(MessageEndEvent(message=res_msg))
