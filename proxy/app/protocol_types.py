"""Typed protocol-neutral models used at gateway governance and translation boundaries.

Wire-protocol models preserve provider-native payloads separately.  These models
represent only semantics the gateway can translate without loss; provider-only
features must remain native or fail capability validation before dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Annotated, Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

JsonObject = dict[str, JsonValue]
JsonSchema = dict[str, JsonValue]
WireProtocol = Literal["openai_chat", "openai_responses", "anthropic_messages"]


def redistribute_redacted_text(original_leaves: list[str], redacted_text: str) -> list[str]:
    """Apply a flattened redaction while preserving text-leaf identity and unaffected text."""
    if not original_leaves:
        return []
    original = "".join(original_leaves)
    if original == redacted_text:
        return list(original_leaves)

    boundaries: list[tuple[int, int]] = []
    cursor = 0
    for leaf in original_leaves:
        boundaries.append((cursor, cursor + len(leaf)))
        cursor += len(leaf)
    output = ["" for _ in original_leaves]

    def leaf_for_position(position: int) -> int:
        for index, (start, end) in enumerate(boundaries):
            if start <= position < end:
                return index
        return len(boundaries) - 1

    for tag, original_start, original_end, redacted_start, redacted_end in SequenceMatcher(
        None, original, redacted_text, autojunk=False
    ).get_opcodes():
        replacement = redacted_text[redacted_start:redacted_end]
        if tag == "insert":
            output[leaf_for_position(original_start)] += replacement
            continue
        replacement_written = False
        for index, (leaf_start, leaf_end) in enumerate(boundaries):
            overlap_start = max(original_start, leaf_start)
            overlap_end = min(original_end, leaf_end)
            if overlap_start >= overlap_end:
                continue
            if tag == "equal":
                offset = overlap_start - original_start
                length = overlap_end - overlap_start
                output[index] += replacement[offset : offset + length]
            elif tag == "replace" and not replacement_written:
                output[index] += replacement
                replacement_written = True
    return output


class ProtocolTranslationError(Exception):
    """A native wire request cannot be translated without losing semantics."""


class GatewayPayload(Protocol):
    """Deep boundary consumed by governance, routing, and provider dispatch."""

    @property
    def protocol(self) -> WireProtocol: ...

    @property
    def native_providers(self) -> frozenset[str]: ...

    @property
    def model(self) -> str: ...

    @property
    def stream(self) -> bool: ...

    def governance_text(self) -> str: ...

    def with_redacted_text(self, redacted_text: str) -> Self: ...

    def native_body(self) -> JsonObject: ...

    def to_chat_body(self) -> JsonObject: ...


class StrictModel(BaseModel):
    """Base for canonical models: unknown semantics are never silently discarded."""

    model_config = ConfigDict(extra="forbid")


CanonicalStreamStatus = Literal["started", "in_progress", "completed", "incomplete", "failed"]
CanonicalStreamTerminalReason = Literal[
    "end_turn",
    "max_tokens",
    "tool_use",
    "content_filtered",
    "cancelled",
    "error",
    "unknown",
]


class CanonicalStreamUsageUpdate(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class CanonicalStreamMessageStarted(StrictModel):
    kind: Literal["message_started"] = "message_started"
    status: Literal["started"] = "started"
    message_id: str = ""
    model: str | None = None


class CanonicalStreamTextDelta(StrictModel):
    kind: Literal["text_delta"] = "text_delta"
    status: Literal["in_progress"] = "in_progress"
    text: str
    output_index: int = Field(default=0, ge=0)
    content_index: int = Field(default=0, ge=0)


class CanonicalStreamToolCallStarted(StrictModel):
    kind: Literal["tool_call_started"] = "tool_call_started"
    status: Literal["in_progress"] = "in_progress"
    tool_index: int = Field(ge=0)
    call_id: str
    name: str


class CanonicalStreamToolCallArgumentsDelta(StrictModel):
    kind: Literal["tool_call_arguments_delta"] = "tool_call_arguments_delta"
    status: Literal["in_progress"] = "in_progress"
    tool_index: int = Field(ge=0)
    arguments_delta: str


class CanonicalStreamUsageUpdated(StrictModel):
    kind: Literal["usage_updated"] = "usage_updated"
    status: Literal["in_progress"] = "in_progress"
    usage: CanonicalStreamUsageUpdate


class CanonicalStreamMessageCompleted(StrictModel):
    kind: Literal["message_completed"] = "message_completed"
    status: Literal["completed", "incomplete"] = "completed"
    reason: CanonicalStreamTerminalReason = "end_turn"


class CanonicalStreamFailed(StrictModel):
    kind: Literal["stream_failed"] = "stream_failed"
    status: Literal["failed"] = "failed"
    reason: Literal["error"] = "error"
    error_type: str
    error_message: str


CanonicalStreamEvent = Annotated[
    CanonicalStreamMessageStarted
    | CanonicalStreamTextDelta
    | CanonicalStreamToolCallStarted
    | CanonicalStreamToolCallArgumentsDelta
    | CanonicalStreamUsageUpdated
    | CanonicalStreamMessageCompleted
    | CanonicalStreamFailed,
    Field(discriminator="kind"),
]


def format_validation_location(location: tuple[str | int, ...]) -> str:
    """Remove Pydantic union implementation labels from a client-facing field path."""
    discriminator_tags = {
        "allowed_tools",
        "assistant",
        "custom",
        "developer",
        "file",
        "function",
        "image_url",
        "input_audio",
        "refusal",
        "system",
        "text",
        "tool",
        "user",
    }
    cleaned: list[str | int] = []
    for index, part in enumerate(location):
        if index > 0 and isinstance(part, str) and (part == "str" or part.startswith("list[")):
            continue
        next_part = location[index + 1] if index + 1 < len(location) else None
        previous_part = location[index - 1] if index > 0 else None
        if part in discriminator_tags and (isinstance(previous_part, int) or next_part == part):
            continue
        cleaned.append(part)
    return ".".join(str(part) for part in cleaned)


class OpenAIChatPromptCacheBreakpoint(StrictModel):
    mode: Literal["explicit"]


class OpenAIChatTextPart(StrictModel):
    type: Literal["text"] = "text"
    text: str
    prompt_cache_breakpoint: OpenAIChatPromptCacheBreakpoint | None = None


class OpenAIChatRefusalPart(StrictModel):
    type: Literal["refusal"] = "refusal"
    refusal: str


class OpenAIChatImageURL(StrictModel):
    url: str
    detail: Literal["auto", "low", "high"] | None = None


class OpenAIChatImagePart(StrictModel):
    type: Literal["image_url"] = "image_url"
    image_url: OpenAIChatImageURL
    prompt_cache_breakpoint: OpenAIChatPromptCacheBreakpoint | None = None


class OpenAIChatInputAudio(StrictModel):
    data: str
    format: Literal["wav", "mp3"]


class OpenAIChatInputAudioPart(StrictModel):
    type: Literal["input_audio"] = "input_audio"
    input_audio: OpenAIChatInputAudio
    prompt_cache_breakpoint: OpenAIChatPromptCacheBreakpoint | None = None


class OpenAIChatFileReference(StrictModel):
    file_data: str | None = None
    file_id: str | None = None
    filename: str | None = None

    @model_validator(mode="after")
    def require_file_source(self) -> OpenAIChatFileReference:
        if self.file_data is None and self.file_id is None:
            raise ValueError("file requires file_data or file_id")
        return self


class OpenAIChatFilePart(StrictModel):
    type: Literal["file"] = "file"
    file: OpenAIChatFileReference
    prompt_cache_breakpoint: OpenAIChatPromptCacheBreakpoint | None = None


OpenAIChatUserContentPart = Annotated[
    OpenAIChatTextPart | OpenAIChatImagePart | OpenAIChatInputAudioPart | OpenAIChatFilePart,
    Field(discriminator="type"),
]
OpenAIChatAssistantContentPart = Annotated[
    OpenAIChatTextPart | OpenAIChatRefusalPart,
    Field(discriminator="type"),
]


class OpenAIChatFunctionCall(StrictModel):
    arguments: str
    name: str


class OpenAIChatCustomCall(StrictModel):
    input: str
    name: str


class OpenAIChatFunctionToolCall(StrictModel):
    id: str
    type: Literal["function"] = "function"
    function: OpenAIChatFunctionCall


class OpenAIChatCustomToolCall(StrictModel):
    id: str
    type: Literal["custom"] = "custom"
    custom: OpenAIChatCustomCall


OpenAIChatMessageToolCall = Annotated[
    OpenAIChatFunctionToolCall | OpenAIChatCustomToolCall,
    Field(discriminator="type"),
]


class OpenAIChatDeveloperMessage(StrictModel):
    role: Literal["developer"] = "developer"
    content: str | list[OpenAIChatTextPart]
    name: str | None = None


class OpenAIChatSystemMessage(StrictModel):
    role: Literal["system"] = "system"
    content: str | list[OpenAIChatTextPart]
    name: str | None = None


class OpenAIChatUserMessage(StrictModel):
    role: Literal["user"] = "user"
    content: str | list[OpenAIChatUserContentPart]
    name: str | None = None


class OpenAIChatAssistantAudioReference(StrictModel):
    id: str


class OpenAIChatAssistantMessage(StrictModel):
    role: Literal["assistant"] = "assistant"
    audio: OpenAIChatAssistantAudioReference | None = None
    content: str | list[OpenAIChatAssistantContentPart] | None = None
    function_call: OpenAIChatFunctionCall | None = None
    name: str | None = None
    refusal: str | None = None
    tool_calls: list[OpenAIChatMessageToolCall] | None = None


class OpenAIChatToolMessage(StrictModel):
    role: Literal["tool"] = "tool"
    content: str | list[OpenAIChatTextPart]
    tool_call_id: str


class OpenAIChatFunctionMessage(StrictModel):
    role: Literal["function"] = "function"
    content: str | None
    name: str


OpenAIChatMessage = Annotated[
    OpenAIChatDeveloperMessage
    | OpenAIChatSystemMessage
    | OpenAIChatUserMessage
    | OpenAIChatAssistantMessage
    | OpenAIChatToolMessage
    | OpenAIChatFunctionMessage,
    Field(discriminator="role"),
]


class OpenAIChatFunctionDefinition(StrictModel):
    name: str
    description: str | None = None
    parameters: JsonSchema | None = None
    strict: bool | None = None


class OpenAIChatFunctionTool(StrictModel):
    type: Literal["function"] = "function"
    function: OpenAIChatFunctionDefinition


class OpenAIChatCustomFormatText(StrictModel):
    type: Literal["text"] = "text"


class OpenAIChatCustomGrammar(StrictModel):
    definition: str
    syntax: Literal["lark", "regex"]


class OpenAIChatCustomFormatGrammar(StrictModel):
    type: Literal["grammar"] = "grammar"
    grammar: OpenAIChatCustomGrammar


OpenAIChatCustomFormat = Annotated[
    OpenAIChatCustomFormatText | OpenAIChatCustomFormatGrammar,
    Field(discriminator="type"),
]


class OpenAIChatCustomToolDefinition(StrictModel):
    name: str
    description: str | None = None
    format: OpenAIChatCustomFormat | None = None


class OpenAIChatCustomTool(StrictModel):
    type: Literal["custom"] = "custom"
    custom: OpenAIChatCustomToolDefinition


OpenAIChatTool = Annotated[
    OpenAIChatFunctionTool | OpenAIChatCustomTool,
    Field(discriminator="type"),
]


class OpenAIChatNamedFunction(StrictModel):
    name: str


class OpenAIChatNamedFunctionChoice(StrictModel):
    type: Literal["function"] = "function"
    function: OpenAIChatNamedFunction


class OpenAIChatNamedCustomChoice(StrictModel):
    type: Literal["custom"] = "custom"
    custom: OpenAIChatNamedFunction


class OpenAIChatAllowedToolReference(StrictModel):
    type: Literal["function", "custom"]
    function: OpenAIChatNamedFunction | None = None
    custom: OpenAIChatNamedFunction | None = None

    @model_validator(mode="after")
    def require_matching_tool(self) -> OpenAIChatAllowedToolReference:
        if self.type == "function" and self.function is not None and self.custom is None:
            return self
        if self.type == "custom" and self.custom is not None and self.function is None:
            return self
        raise ValueError("allowed tool reference must match its type")


class OpenAIChatAllowedTools(StrictModel):
    mode: Literal["auto", "required"]
    tools: list[OpenAIChatAllowedToolReference]


class OpenAIChatAllowedToolChoice(StrictModel):
    type: Literal["allowed_tools"] = "allowed_tools"
    allowed_tools: OpenAIChatAllowedTools


OpenAIChatObjectToolChoice = Annotated[
    OpenAIChatNamedFunctionChoice | OpenAIChatNamedCustomChoice | OpenAIChatAllowedToolChoice,
    Field(discriminator="type"),
]


class OpenAIChatStreamOptions(StrictModel):
    include_obfuscation: bool | None = None
    include_usage: bool | None = None


class OpenAIChatAudioVoiceID(StrictModel):
    id: str


class OpenAIChatAudioConfig(StrictModel):
    format: Literal["wav", "aac", "mp3", "flac", "opus", "pcm16"]
    voice: str | OpenAIChatAudioVoiceID


class OpenAIChatPrediction(StrictModel):
    type: Literal["content"] = "content"
    content: str | list[OpenAIChatTextPart]


class OpenAIChatPromptCacheOptions(StrictModel):
    mode: Literal["implicit", "explicit"] | None = None
    ttl: Literal["30m"] | None = None


class OpenAIChatModerationPolicyMode(StrictModel):
    mode: Literal["score", "block"]


class OpenAIChatModerationPolicy(StrictModel):
    input: OpenAIChatModerationPolicyMode | None = None
    output: OpenAIChatModerationPolicyMode | None = None


class OpenAIChatModerationConfig(StrictModel):
    model: str
    policy: OpenAIChatModerationPolicy | None = None


class OpenAIChatResponseFormatText(StrictModel):
    type: Literal["text"] = "text"


class OpenAIChatResponseFormatJSONObject(StrictModel):
    type: Literal["json_object"] = "json_object"


class OpenAIChatJSONSchemaConfig(StrictModel):
    name: str
    description: str | None = None
    schema_: JsonSchema | None = Field(default=None, alias="schema")
    strict: bool | None = None


class OpenAIChatResponseFormatJSONSchema(StrictModel):
    type: Literal["json_schema"] = "json_schema"
    json_schema: OpenAIChatJSONSchemaConfig


OpenAIChatResponseFormat = Annotated[
    OpenAIChatResponseFormatText
    | OpenAIChatResponseFormatJSONObject
    | OpenAIChatResponseFormatJSONSchema,
    Field(discriminator="type"),
]


class OpenAIChatApproximateLocation(StrictModel):
    city: str | None = None
    country: str | None = None
    region: str | None = None
    timezone: str | None = None


class OpenAIChatUserLocation(StrictModel):
    type: Literal["approximate"] = "approximate"
    approximate: OpenAIChatApproximateLocation


class OpenAIChatWebSearchOptions(StrictModel):
    search_context_size: Literal["low", "medium", "high"] | None = None
    user_location: OpenAIChatUserLocation | None = None


class OpenAIChatRequest(StrictModel):
    """Strict local DTO for the current OpenAI Chat Completions request envelope."""

    model: str
    messages: list[OpenAIChatMessage]
    audio: OpenAIChatAudioConfig | None = None
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    function_call: Literal["none", "auto"] | OpenAIChatNamedFunction | None = None
    functions: list[OpenAIChatFunctionDefinition] | None = None
    logit_bias: dict[str, int] | None = None
    logprobs: bool | None = None
    max_completion_tokens: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    metadata: dict[str, str] | None = None
    modalities: list[Literal["text", "audio"]] | None = None
    moderation: OpenAIChatModerationConfig | None = None
    n: int | None = Field(default=None, ge=1)
    parallel_tool_calls: bool | None = None
    prediction: OpenAIChatPrediction | None = None
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    prompt_cache_key: str | None = None
    prompt_cache_options: OpenAIChatPromptCacheOptions | None = None
    prompt_cache_retention: Literal["in_memory", "24h"] | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    response_format: OpenAIChatResponseFormat | None = None
    safety_identifier: str | None = None
    seed: int | None = None
    service_tier: Literal["auto", "default", "flex", "scale", "priority", "fast"] | None = None
    stop: str | list[str] | None = None
    store: bool | None = None
    stream: bool = False
    stream_options: OpenAIChatStreamOptions | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    tool_choice: Literal["none", "auto", "required"] | OpenAIChatObjectToolChoice | None = None
    tools: list[OpenAIChatTool] | None = None
    top_logprobs: int | None = Field(default=None, ge=0, le=20)
    top_p: float | None = Field(default=None, ge=0, le=1)
    user: str | None = None
    verbosity: Literal["low", "medium", "high"] | None = None
    web_search_options: OpenAIChatWebSearchOptions | None = None

    @model_validator(mode="after")
    def validate_conditional_fields(self) -> OpenAIChatRequest:
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        if self.top_logprobs is not None and not self.logprobs:
            raise ValueError("top_logprobs requires logprobs=true")
        if self.modalities is not None and "audio" in self.modalities and self.audio is None:
            raise ValueError("audio configuration is required for audio output")
        return self

    def to_json(self) -> JsonObject:
        return cast(
            JsonObject,
            self.model_dump(mode="json", by_alias=True, exclude_unset=True),
        )


@dataclass(frozen=True)
class _OpenAIChatGovernedLeaf:
    message_index: int
    part_index: int | None
    text: str


def _openai_chat_governed_leaves(
    messages: list[OpenAIChatMessage],
) -> list[_OpenAIChatGovernedLeaf]:
    for message_index in range(len(messages) - 1, -1, -1):
        message = messages[message_index]
        if not isinstance(message, OpenAIChatUserMessage):
            continue
        if isinstance(message.content, str):
            return [_OpenAIChatGovernedLeaf(message_index, None, message.content)]
        return [
            _OpenAIChatGovernedLeaf(message_index, part_index, part.text)
            for part_index, part in enumerate(message.content)
            if isinstance(part, OpenAIChatTextPart)
        ]
    return []


@dataclass(frozen=True)
class OpenAIChatPayload:
    """Typed ownership wrapper for a validated OpenAI Chat request."""

    request: OpenAIChatRequest
    protocol: WireProtocol = "openai_chat"
    native_providers: frozenset[str] = frozenset({"generic", "ollama", "openai"})

    @property
    def model(self) -> str:
        return self.request.model

    @property
    def stream(self) -> bool:
        return self.request.stream

    def governance_text(self) -> str:
        return "".join(leaf.text for leaf in _openai_chat_governed_leaves(self.request.messages))

    def with_redacted_text(self, redacted_text: str) -> OpenAIChatPayload:
        messages = list(self.request.messages)
        leaves = _openai_chat_governed_leaves(messages)
        if leaves:
            message_index = leaves[0].message_index
            message = messages[message_index]
            assert isinstance(message, OpenAIChatUserMessage)
            replacements = iter(redistribute_redacted_text([leaf.text for leaf in leaves], redacted_text))
            if isinstance(message.content, str):
                messages[message_index] = message.model_copy(update={"content": next(replacements)})
            else:
                replacement_by_part = {
                    leaf.part_index: replacement
                    for leaf, replacement in zip(leaves, replacements, strict=True)
                }
                content = [
                    part.model_copy(update={"text": replacement_by_part[part_index]})
                    if part_index in replacement_by_part and isinstance(part, OpenAIChatTextPart)
                    else part
                    for part_index, part in enumerate(message.content)
                ]
                messages[message_index] = message.model_copy(update={"content": content})
        request = self.request.model_copy(update={"messages": messages})
        return OpenAIChatPayload(request=request)

    def native_body(self) -> JsonObject:
        return self.request.to_json()

    def to_chat_body(self) -> JsonObject:
        return self.request.to_json()


class OpenAIChatTokenTopLogprob(StrictModel):
    token: str
    bytes: list[int] | None = None
    logprob: float


class OpenAIChatTokenLogprob(StrictModel):
    token: str
    bytes: list[int] | None = None
    logprob: float
    top_logprobs: list[OpenAIChatTokenTopLogprob]


class OpenAIChatChoiceLogprobs(StrictModel):
    content: list[OpenAIChatTokenLogprob] | None = None
    refusal: list[OpenAIChatTokenLogprob] | None = None


class OpenAIChatChunkFunctionDelta(StrictModel):
    arguments: str | None = None
    name: str | None = None


class OpenAIChatChunkToolCall(StrictModel):
    index: int = Field(ge=0)
    id: str | None = None
    function: OpenAIChatChunkFunctionDelta | None = None
    type: Literal["function"] | None = None


class OpenAIChatChunkDelta(StrictModel):
    content: str | None = None
    function_call: OpenAIChatChunkFunctionDelta | None = None
    refusal: str | None = None
    role: Literal["developer", "system", "user", "assistant", "tool"] | None = None
    tool_calls: list[OpenAIChatChunkToolCall] | None = None


class OpenAIChatChunkChoice(StrictModel):
    delta: OpenAIChatChunkDelta
    finish_reason: Literal[
        "stop", "length", "tool_calls", "content_filter", "function_call"
    ] | None = None
    index: int = Field(ge=0)
    logprobs: OpenAIChatChoiceLogprobs | None = None


class OpenAIChatUsageCompletionDetails(StrictModel):
    accepted_prediction_tokens: int | None = Field(default=None, ge=0)
    audio_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    rejected_prediction_tokens: int | None = Field(default=None, ge=0)


class OpenAIChatUsagePromptDetails(StrictModel):
    audio_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)


class OpenAIChatChunkUsage(StrictModel):
    completion_tokens: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    completion_tokens_details: OpenAIChatUsageCompletionDetails | None = None
    prompt_tokens_details: OpenAIChatUsagePromptDetails | None = None


class OpenAIChatModerationResult(StrictModel):
    categories: dict[str, bool]
    category_applied_input_types: dict[str, list[Literal["text", "image"]]]
    category_scores: dict[str, float]
    flagged: bool
    model: str
    type: Literal["moderation_result"] = "moderation_result"


class OpenAIChatModerationResults(StrictModel):
    model: str
    results: list[OpenAIChatModerationResult]
    type: Literal["moderation_results"] = "moderation_results"


class OpenAIChatModerationError(StrictModel):
    code: str
    message: str
    type: Literal["error"] = "error"


OpenAIChatModerationOutcome = Annotated[
    OpenAIChatModerationResults | OpenAIChatModerationError,
    Field(discriminator="type"),
]


class OpenAIChatModeration(StrictModel):
    input: OpenAIChatModerationOutcome
    output: OpenAIChatModerationOutcome


class OpenAIChatCompletionChunk(StrictModel):
    id: str
    choices: list[OpenAIChatChunkChoice]
    created: int
    model: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    moderation: OpenAIChatModeration | None = None
    service_tier: Literal["auto", "default", "flex", "scale", "priority", "fast"] | None = None
    system_fingerprint: str | None = None
    usage: OpenAIChatChunkUsage | None = None


class OpenAIChatResponseMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str = "assistant"
    content: str | list[JsonObject] | None = None
    tool_calls: list[JsonObject] | None = None


class OpenAIChatResponseChoice(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = 0
    message: OpenAIChatResponseMessage
    finish_reason: str | None = None


class OpenAIChatResponseUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OpenAIChatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = "chatcmpl-generated"
    created: int | float = 0
    model: str = ""
    choices: list[OpenAIChatResponseChoice]
    usage: OpenAIChatResponseUsage = Field(default_factory=OpenAIChatResponseUsage)


class ExecutionInputText(StrictModel):
    type: Literal["input_text"] = "input_text"
    text: str


class ExecutionInputImage(StrictModel):
    type: Literal["input_image"] = "input_image"
    image_url: str | None = None
    file_id: str | None = None
    detail: Literal["auto", "low", "high"] = "auto"

    @model_validator(mode="after")
    def require_one_image_source(self) -> ExecutionInputImage:
        if (self.image_url is None) == (self.file_id is None):
            raise ValueError("input_image requires exactly one of image_url or file_id")
        return self


class ExecutionInputFile(StrictModel):
    type: Literal["input_file"] = "input_file"
    file_id: str | None = None
    file_url: str | None = None
    filename: str | None = None
    file_data: str | None = None


ExecutionInputContent = Annotated[
    ExecutionInputText | ExecutionInputImage | ExecutionInputFile,
    Field(discriminator="type"),
]


class ExecutionFunctionCallItem(StrictModel):
    type: Literal["function_call"] = "function_call"
    call_id: str
    name: str
    arguments: str
    id: str | None = None
    status: Literal["in_progress", "completed", "incomplete"] | None = None


class ExecutionFunctionCallOutputItem(StrictModel):
    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    output: str | list[ExecutionInputContent]
    id: str | None = None
    status: Literal["in_progress", "completed", "incomplete"] | None = None


class ExecutionReasoningSummary(StrictModel):
    type: Literal["summary_text"] = "summary_text"
    text: str


class ExecutionReasoningContent(StrictModel):
    type: Literal["reasoning_text"] = "reasoning_text"
    text: str


class ExecutionReasoningItem(StrictModel):
    type: Literal["reasoning"] = "reasoning"
    id: str | None = None
    summary: list[ExecutionReasoningSummary] = Field(default_factory=list)
    content: list[ExecutionReasoningContent] | None = None
    encrypted_content: str | None = None
    status: Literal["in_progress", "completed", "incomplete"] | None = None


class ExecutionItemReference(StrictModel):
    type: Literal["item_reference"] = "item_reference"
    id: str
