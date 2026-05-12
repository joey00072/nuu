"""
Generic async event stream with queue-based iteration and final-result extraction.
Specialized as AssistantMessageEventStream for LLM response streaming.

Owns: EventStream[T, R] async generator pattern, AssistantMessageEventStream.
Delegates to: asyncio.Queue for back-pressure and coroutine orchestration.

Data flow: push(event) -> async for event in stream -> result() -> R
  End-of-stream is signaled via None sentinel in the queue.

Depends on: asyncio, nuu.ai.types (AssistantMessage, AssistantMessageEvent)
"""

import asyncio
from typing import AsyncIterable, Callable, Generic, TypeVar

from .types import AssistantMessage, AssistantMessageEvent

T = TypeVar("T")
R = TypeVar("R")


class EventStream(Generic[T, R]):
    def __init__(
        self,
        is_complete: Callable[[T], bool],
        extract_result: Callable[[T], R],
    ):
        self._queue: asyncio.Queue[T | None] = asyncio.Queue()
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._done = False
        self._final_result_future: asyncio.Future[R] = (
            asyncio.get_event_loop().create_future()
        )

    def push(self, event: T) -> None:
        if self._done:
            return

        if self._is_complete(event):
            self._done = True
            self._final_result_future.set_result(self._extract_result(event))
            self._queue.put_nowait(event)
            self._queue.put_nowait(None)  # Sentinel to end iteration
        else:
            self._queue.put_nowait(event)

    def end(self, result: R | None = None) -> None:
        if self._done:
            return
        self._done = True
        if result is not None:
            self._final_result_future.set_result(result)
        elif not self._final_result_future.done():
            # If no result provided and future not set, we might have an issue
            # but for now just end it.
            pass
        self._queue.put_nowait(None)

    async def __aiter__(self) -> AsyncIterable[T]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event

    async def result(self) -> R:
        return await self._final_result_future


class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    def __init__(self):
        super().__init__(
            is_complete=lambda event: event["type"] in ("done", "error"),
            extract_result=self._extract_assistant_message,
        )

    def _extract_assistant_message(
        self, event: AssistantMessageEvent
    ) -> AssistantMessage:
        if event["type"] == "done":
            return event["message"]
        elif event["type"] == "error":
            return event["error"]
        raise ValueError(f"Unexpected event type for final result: {event['type']}")
