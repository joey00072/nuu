"""
Typed event bus for decoupled pub-sub communication within the coding agent.
Supports typed events with TypeVar-based generics for type-safe subscriptions.

Owns: EventBus[T] class.
Delegates to: caller for event production and consumption.

Depends on: standard library only (typing)
"""

from __future__ import annotations

from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class EventBus(Generic[T]):
    def __init__(self) -> None:
        self._handlers: list[Callable[[T], None]] = []

    def emit(self, event: T) -> None:
        for handler in self._handlers:
            try:
                handler(event)
            except Exception as e:
                print(f"Event handler error: {e}")

    def on(self, handler: Callable[[T], None]) -> Callable[[], None]:
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler)

    def off(self, handler: Callable[[T], None]) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    def clear(self) -> None:
        self._handlers.clear()


def create_event_bus() -> EventBus:
    return EventBus()
