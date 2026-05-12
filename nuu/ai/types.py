"""
Shared Pydantic models, TypedDict events, enums, and type aliases for the AI
provider layer. Single source of truth for message shapes, event types,
model metadata structures, and compatibility flags.

Owns: KnownApi, KnownProvider, ModelInfo, Message types, AssistantMessageEvent
  TypedDicts, streaming option models.
Delegates to: pydantic for validation and serialization.

Data flow: JSON/config -> ModelInfo; provider stream -> AssistantMessageEvent;
  AgentContext -> Message list -> Provider stream function.

Depends on: pydantic, standard library only (no nuu imports).
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, TypedDict, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pydantic.alias_generators import to_camel

# ============================================================================
# API and Provider Types
# ============================================================================


class KnownApi(str, Enum):
    OPENAI_COMPLETIONS = "openai-completions"
    MISTRAL_CONVERSATIONS = "mistral-conversations"
    OPENAI_RESPONSES = "openai-responses"
    AZURE_OPENAI_RESPONSES = "azure-openai-responses"
    OPENAI_CODEX_RESPONSES = "openai-codex-responses"
    ANTHROPIC_MESSAGES = "anthropic-messages"
    BEDROCK_CONVERSE_STREAM = "bedrock-converse-stream"
    GOOGLE_GENERATIVE_AI = "google-generative-ai"
    GOOGLE_VERTEX = "google-vertex"


Api = Union[KnownApi, str]


class KnownProvider(str, Enum):
    AMAZON_BEDROCK = "amazon-bedrock"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    GOOGLE_VERTEX = "google-vertex"
    OPENAI = "openai"
    AZURE_OPENAI_RESPONSES = "azure-openai-responses"
    OPENAI_CODEX = "openai-codex"
    DEEPSEEK = "deepseek"
    GITHUB_COPILOT = "github-copilot"
    XAI = "xai"
    GROQ = "groq"
    CEREBRAS = "cerebras"
    OPENROUTER = "openrouter"
    VERCEL_AI_GATEWAY = "vercel-ai-gateway"
    ZAI = "zai"
    MISTRAL = "mistral"
    MINIMAX = "minimax"
    MINIMAX_CN = "minimax-cn"
    MOONSHOTAI = "moonshotai"
    MOONSHOTAI_CN = "moonshotai-cn"
    HUGGINGFACE = "huggingface"
    FIREWORKS = "fireworks"
    OPENCODE = "opencode"
    OPENCODE_GO = "opencode-go"
    KIMI_CODING = "kimi-coding"
    CLOUDFLARE_WORKERS_AI = "cloudflare-workers-ai"
    CLOUDFLARE_AI_GATEWAY = "cloudflare-ai-gateway"
    XIAOMI = "xiaomi"
    XIAOMI_TOKEN_PLAN_CN = "xiaomi-token-plan-cn"
    XIAOMI_TOKEN_PLAN_AMS = "xiaomi-token-plan-ams"
    XIAOMI_TOKEN_PLAN_SGP = "xiaomi-token-plan-sgp"


Provider = Union[KnownProvider, str]

# ============================================================================
# Thinking and Options
# ============================================================================

ThinkingLevel = Literal["minimal", "low", "medium", "high", "xhigh"]
ModelThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]
ThinkingLevelMap = dict[ModelThinkingLevel, Union[str, None]]


class ThinkingBudgets(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    minimal: int | None = None
    low: int | None = None
    medium: int | None = None
    high: int | None = None


CacheRetention = Literal["none", "short", "long"]
Transport = Literal["sse", "websocket", "websocket-cached", "auto"]


class ProviderResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    status: int
    headers: dict[str, str]


class PiBaseModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        arbitrary_types_allowed=True,
        extra="allow",
    )


class StreamOptions(PiBaseModel):
    temperature: float | None = None
    max_tokens: int | None = None
    # signal: AbortSignal | None = None  # Handled via asyncio.Task.cancel or explicit check
    api_key: str | None = None
    transport: Transport | None = None
    cache_retention: CacheRetention | None = None
    session_id: str | None = None
    # on_payload: Callable[[Any, ModelInfo], Any] | None = None
    # on_response: Callable[[ProviderResponse, ModelInfo], None] | None = None
    headers: dict[str, str] | None = None
    timeout_ms: int | None = None
    max_retries: int | None = None
    max_retry_delay_ms: int | None = None
    metadata: dict[str, Any] | None = None


class SimpleStreamOptions(StreamOptions):
    reasoning: ThinkingLevel | None = None
    thinking_budgets: ThinkingBudgets | None = None


# ============================================================================
# Message Content
# ============================================================================


class TextSignatureV1(PiBaseModel):
    v: Literal[1] = 1
    id: str
    phase: Literal["commentary", "final_answer"] | None = None


class TextContent(PiBaseModel):
    type: Literal["text"] = "text"
    text: str
    _index: int | None = PrivateAttr(default=None)
    _partial_json: str | None = PrivateAttr(default=None)
    text_signature: str | None = None


class ThinkingContent(PiBaseModel):
    type: Literal["thinking"] = "thinking"
    thinking: str
    thinking_signature: str | None = None
    redacted: bool | None = None


class ImageContent(PiBaseModel):
    type: Literal["image"] = "image"
    data: str  # base64 encoded image data
    mime_type: str


class ToolCall(PiBaseModel):
    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, Any]
    thought_signature: str | None = None
    partial_args: str | None = Field(default=None, exclude=True)
    stream_index: int | None = Field(default=None, exclude=True)


# ============================================================================
# Usage and Messages
# ============================================================================


class UsageCost(PiBaseModel):
    input: float
    output: float
    cache_read: float
    cache_write: float
    total: float


class Usage(PiBaseModel):
    input: int
    output: int
    cache_read: int
    cache_write: int
    total_tokens: int
    cost: UsageCost


StopReason = Literal["stop", "length", "toolUse", "error", "aborted"]

UserContentBlock = Annotated[Union[TextContent, ImageContent], Field(discriminator="type")]
AssistantContentBlock = Annotated[Union[TextContent, ThinkingContent, ToolCall], Field(discriminator="type")]
ToolResultContentBlock = Annotated[Union[TextContent, ImageContent], Field(discriminator="type")]

class UserMessage(PiBaseModel):
    role: Literal["user"] = "user"
    content: str | list[Union[TextContent, ImageContent]]
    timestamp: int


class AssistantMessageDiagnostic(PiBaseModel):
    type: str
    message: str
    details: Any | None = None


class AssistantMessage(PiBaseModel):
    role: Literal["assistant"] = "assistant"
    content: list[AssistantContentBlock]
    api: Api
    provider: Provider
    model: str
    response_model: str | None = None
    response_id: str | None = None
    diagnostics: list[AssistantMessageDiagnostic] | None = None
    usage: Usage
    stop_reason: StopReason
    error_message: str | None = None
    timestamp: int


class ToolResultMessage(PiBaseModel):
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    tool_name: str
    content: list[ToolResultContentBlock]
    details: Any | None = None
    is_error: bool
    timestamp: int


Message = Annotated[Union[UserMessage, AssistantMessage, ToolResultMessage], Field(discriminator="role")]


class Tool(PiBaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class Context(PiBaseModel):
    system_prompt: str | None = None
    messages: list[Message]
    tools: list[Tool] | None = None


# ============================================================================
# Events
# ============================================================================


class StartEvent(TypedDict):
    type: Literal["start"]
    partial: AssistantMessage


class TextStartEvent(TypedDict):
    type: Literal["text_start"]
    contentIndex: int
    partial: AssistantMessage


class TextDeltaEvent(TypedDict):
    type: Literal["text_delta"]
    contentIndex: int
    delta: str
    partial: AssistantMessage


class TextEndEvent(TypedDict):
    type: Literal["text_end"]
    contentIndex: int
    content: str
    partial: AssistantMessage


class ThinkingStartEvent(TypedDict):
    type: Literal["thinking_start"]
    contentIndex: int
    partial: AssistantMessage


class ThinkingDeltaEvent(TypedDict):
    type: Literal["thinking_delta"]
    contentIndex: int
    delta: str
    partial: AssistantMessage


class ThinkingEndEvent(TypedDict):
    type: Literal["thinking_end"]
    contentIndex: int
    content: str
    partial: AssistantMessage


class ToolCallStartEvent(TypedDict):
    type: Literal["toolcall_start"]
    contentIndex: int
    partial: AssistantMessage


class ToolCallDeltaEvent(TypedDict):
    type: Literal["toolcall_delta"]
    contentIndex: int
    delta: str
    partial: AssistantMessage


class ToolCallEndEvent(TypedDict):
    type: Literal["toolcall_end"]
    contentIndex: int
    toolCall: ToolCall
    partial: AssistantMessage


class DoneEvent(TypedDict):
    type: Literal["done"]
    reason: Literal["stop", "length", "toolUse"]
    message: AssistantMessage


class ErrorEvent(TypedDict):
    type: Literal["error"]
    reason: Literal["aborted", "error"]
    error: AssistantMessage


AssistantMessageEvent = Union[
    StartEvent,
    TextStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    ThinkingStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ToolCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    DoneEvent,
    ErrorEvent,
]

# ============================================================================
# Compatibility and Routing
# ============================================================================


class OpenRouterRouting(PiBaseModel):
    allow_fallbacks: bool | None = None
    require_parameters: bool | None = None
    data_collection: Literal["deny", "allow"] | None = None
    zdr: bool | None = None
    enforce_distillable_text: bool | None = None
    order: list[str] | None = None
    only: list[str] | None = None
    ignore: list[str] | None = None
    quantizations: list[str] | None = None
    sort: Union[str, dict[str, Any]] | None = None
    max_price: dict[str, Any] | None = None
    preferred_min_throughput: Union[float, dict[str, Any]] | None = None
    preferred_max_latency: Union[float, dict[str, Any]] | None = None


class VercelGatewayRouting(PiBaseModel):
    only: list[str] | None = None
    order: list[str] | None = None


class OpenAICompletionsCompat(PiBaseModel):
    supports_store: bool | None = None
    supports_developer_role: bool | None = None
    supports_reasoning_effort: bool | None = None
    supports_usage_in_streaming: bool | None = None
    max_tokens_field: Literal["max_completion_tokens", "max_tokens"] | None = None
    requires_tool_result_name: bool | None = None
    requires_assistant_after_tool_result: bool | None = None
    requires_thinking_as_text: bool | None = None
    requires_reasoning_content_on_assistant_messages: bool | None = None
    thinking_format: (
        Literal["openai", "openrouter", "deepseek", "zai", "qwen", "qwen-chat-template"]
        | None
    ) = None
    open_router_routing: OpenRouterRouting | None = None
    vercel_gateway_routing: VercelGatewayRouting | None = None
    zai_tool_stream: bool | None = None
    supports_strict_mode: bool | None = None
    cache_control_format: Literal["anthropic"] | None = None
    send_session_affinity_headers: bool | None = None
    supports_long_cache_retention: bool | None = None


class OpenAIResponsesCompat(PiBaseModel):
    send_session_id_header: bool | None = None
    supports_long_cache_retention: bool | None = None


class AnthropicMessagesCompat(PiBaseModel):
    supports_eager_tool_input_streaming: bool | None = None
    supports_long_cache_retention: bool | None = None


class ModelCost(PiBaseModel):
    input: float
    output: float
    cache_read: float
    cache_write: float


class ModelInfo(PiBaseModel):
    id: str
    name: str
    api: Api
    provider: Provider
    base_url: str
    reasoning: bool
    thinking_level_map: ThinkingLevelMap | None = None
    input: list[Literal["text", "image"]]
    cost: ModelCost
    context_window: int
    max_tokens: int
    headers: dict[str, str] | None = None
    compat: (
        OpenAICompletionsCompat | OpenAIResponsesCompat | AnthropicMessagesCompat | None
    ) = None
