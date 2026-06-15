from typing import Any, Literal

from pydantic import BaseModel, Field


class TextContentBlock(BaseModel):
    type: Literal["text"]
    text: str


class GenericContentBlock(BaseModel):
    type: str

    model_config = {"extra": "allow"}


MessageContent = str | list[TextContentBlock | GenericContentBlock]


class ChatMessage(BaseModel):
    role: str
    content: MessageContent | None = None


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None


class CompletionMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: CompletionMessage
    finish_reason: str = "stop"


class CompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: CompletionUsage


class ModelPermission(BaseModel):
    id: str = "modelperm-codebuddy"
    object: Literal["model_permission"] = "model_permission"
    created: int
    allow_create_engine: bool = False
    allow_sampling: bool = True
    allow_logprobs: bool = False
    allow_search_indices: bool = False
    allow_view: bool = True
    allow_fine_tuning: bool = False
    organization: str = "*"
    group: str | None = None
    is_blocking: bool = False


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "codebuddy"
    permission: list[ModelPermission]


class ModelsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]

# Tool call models for ACP support
class FunctionCall(BaseModel):
    name: str
    arguments: str  # JSON string

class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall

# Extended message models for tool calls and reasoning
class ExtendedCompletionMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    reasoning_content: str | None = None

class ExtendedChatCompletionChoice(BaseModel):
    index: int = 0
    message: ExtendedCompletionMessage
    finish_reason: str = "stop"

class ExtendedChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ExtendedChatCompletionChoice]
    usage: CompletionUsage

# Streaming delta models
class DeltaToolCall(BaseModel):
    index: int
    id: str | None = None
    type: Literal["function"] | None = None
    function: dict[str, Any] | None = None

class DeltaMessage(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None
    tool_calls: list[DeltaToolCall] | None = None
    reasoning_content: str | None = None

class StreamChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: str | None = None

class StreamChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[StreamChoice]
