import pytest
from nuu.agent.agent import Agent
from nuu.agent.types import AgentTool, AgentToolResult
from nuu.ai.providers.faux import (
    register_faux_provider,
    faux_assistant_message,
    faux_tool_call,
)
from nuu.ai.types import UserMessage, TextContent


class MockTool(AgentTool):
    def __init__(self):
        self.name = "hello_tool"
        self.description = "Says hello"
        self.parameters = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        self.label = "Hello Tool"

    async def execute(self, tool_call_id, params, on_update=None):
        return AgentToolResult(
            content=[TextContent(type="text", text=f"Hello, {params['name']}!")],
            details={"name": params["name"]},
        )


@pytest.mark.asyncio
async def test_agent_basic():
    faux = register_faux_provider()
    model = faux.get_model()

    agent = Agent(initial_state={"model": model})

    faux.set_responses([faux_assistant_message("Hello from the agent!")])

    await agent.prompt(UserMessage(content="Hi", timestamp=0))

    assert len(agent.messages) == 2
    assert agent.messages[1].content[0].text == "Hello from the agent!"


@pytest.mark.asyncio
async def test_agent_with_tool():
    faux = register_faux_provider()
    model = faux.get_model()

    tool = MockTool()
    agent = Agent(initial_state={"model": model, "tools": [tool]})

    faux.set_responses(
        [
            faux_assistant_message(
                [faux_tool_call("hello_tool", {"name": "Joey"}, tool_id="call_1")]
            ),
            faux_assistant_message("Tool executed!"),
        ]
    )

    await agent.prompt(UserMessage(content="Run tool", timestamp=0))

    # User message, Assistant (tool call), Tool result, Assistant (final)
    assert len(agent.messages) == 4
    assert agent.messages[1].content[0].type == "toolCall"
    assert agent.messages[2].role == "toolResult"
    assert agent.messages[2].content[0].text == "Hello, Joey!"
    assert agent.messages[3].content[0].text == "Tool executed!"
