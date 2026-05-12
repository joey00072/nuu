from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from ..agent.agent import Agent
from ..agent.types import (
    AgentEvent,
    AgentMessage,
    AgentTool,
    MessageEndEvent,
)
from ..ai.types import ModelInfo, UserMessage, TextContent
from .tools.index import create_all_tools
from .session_manager import SessionManager


def _resolve_api_key(provider: str) -> str | None:
    from ..ai.env_api_keys import get_env_api_key

    key = get_env_api_key(provider)
    if key:
        return key
    auth_file = os.path.expanduser("~/.nuu/auth.json")
    if os.path.exists(auth_file):
        try:
            with open(auth_file) as f:
                stored = json.load(f)
            return stored.get(provider)
        except Exception:
            pass
    return None


class AgentSession:
    def __init__(
        self,
        cwd: str,
        model: ModelInfo,
        system_prompt: str = "",
        session_manager: SessionManager | None = None,
        tools: list[AgentTool[Any]] | None = None,
    ):
        self.cwd = cwd
        self.session_manager = session_manager or SessionManager.create(cwd)

        initial_messages = self.session_manager.build_session_context()

        self.agent = Agent(
            initial_state={
                "model": model,
                "system_prompt": system_prompt,
                "tools": tools or create_all_tools(cwd),
                "messages": initial_messages,
            },
            get_api_key=_resolve_api_key,
        )

        # Subscribe to agent events for persistence
        self.agent.subscribe(self._handle_agent_event)

    def subscribe(self, listener: Callable[[AgentEvent], Any]):
        return self.agent.subscribe(listener)

    async def prompt(self, text: str):
        msg = UserMessage(
            role="user",
            content=[TextContent(type="text", text=text)],
            timestamp=int(time.time() * 1000),
        )
        self.session_manager.append_message(msg)
        await self.agent.prompt(msg)

    def _handle_agent_event(self, event: AgentEvent):
        if isinstance(event, MessageEndEvent):
            if getattr(event.message, "role", None) in ("assistant", "toolResult"):
                self.session_manager.append_message(event.message)

    @property
    def messages(self) -> list[AgentMessage]:
        return self.agent.messages
