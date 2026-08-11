"""Anthropic Messages API compatibility types and translation helpers for Claude Code support."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal

from proxy.app.openai_chat_stream import iter_openai_chat_events
from proxy.app.protocol_types import (
    JsonObject,
    JsonSchema,
    OpenAIChatResponse,
    ProtocolTranslationError,
    WireProtocol,
    redistribute_redacted_text,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class AnthropicCompatError(ProtocolTranslationError):
    """Raised when a request uses an unsupported Anthropic Messages shape."""


class AnthropicCacheControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ephemeral"]
    ttl: Literal["5m", "1h"] | None = None


class AnthropicTextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str
    cache_control: AnthropicCacheControl | None = None


class AnthropicImageSourceBase64(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["base64"] = "base64"
    media_type: str
    data: str


class AnthropicImageSourceUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["url"] = "url"
    url: str


AnthropicImageSource = Annotated[
    AnthropicImageSourceBase64 | AnthropicImageSourceUrl,
    Field(discriminator="type"),
]


class AnthropicImageBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image"] = "image"
    source: AnthropicImageSource
    cache_control: AnthropicCacheControl | None = None


class AnthropicToolUseBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: JsonObject
    cache_control: AnthropicCacheControl | None = None


AnthropicToolResultContentBlock = Annotated[
    AnthropicTextBlock | AnthropicImageBlock,
    Field(discriminator="type"),
]


class AnthropicToolResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[AnthropicToolResultContentBlock] | None = None
    is_error: bool | None = None
    cache_control: AnthropicCacheControl | None = None


class AnthropicThinkingBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str


class AnthropicRedactedThinkingBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str


AnthropicContentBlock = Annotated[
    AnthropicTextBlock
    | AnthropicImageBlock
    | AnthropicToolUseBlock
    | AnthropicToolResultBlock
    | AnthropicThinkingBlock
    | AnthropicRedactedThinkingBlock,
    Field(discriminator="type"),
]

AnthropicContent = str | list[AnthropicContentBlock]


class AnthropicMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant", "system"]
    content: AnthropicContent


class AnthropicThinkingEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["enabled"]
    budget_tokens: int = Field(ge=1024)
    display: Literal["summarized", "omitted"] | None = None


class AnthropicThinkingDisabled(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["disabled"]


class AnthropicThinkingAdaptive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["adaptive"]
    display: Literal["summarized", "omitted"] | None = None


AnthropicThinking = Annotated[
    AnthropicThinkingEnabled | AnthropicThinkingDisabled | AnthropicThinkingAdaptive,
    Field(discriminator="type"),
]


class AnthropicMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None


class AnthropicInputTokensThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["input_tokens"]
    value: int


class AnthropicToolUsesThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_uses"]
    value: int


class AnthropicThinkingTurns(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["thinking_turns"]
    value: int


class AnthropicAllThinkingTurns(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["all"]


AnthropicContextTrigger = Annotated[
    AnthropicInputTokensThreshold | AnthropicToolUsesThreshold,
    Field(discriminator="type"),
]


class AnthropicClearToolUsesEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["clear_tool_uses_20250919"]
    clear_at_least: AnthropicInputTokensThreshold | None = None
    clear_tool_inputs: bool | list[str] | None = None
    exclude_tools: list[str] | None = None
    keep: AnthropicToolUsesThreshold | None = None
    trigger: AnthropicContextTrigger | None = None


class AnthropicClearThinkingEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["clear_thinking_20251015"]
    keep: AnthropicThinkingTurns | AnthropicAllThinkingTurns | Literal["all"] | None = None


class AnthropicCompactEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["compact_20260112"]
    instructions: str | None = None
    pause_after_compaction: bool | None = None
    trigger: AnthropicInputTokensThreshold | None = None


AnthropicContextEdit = Annotated[
    AnthropicClearToolUsesEdit | AnthropicClearThinkingEdit | AnthropicCompactEdit,
    Field(discriminator="type"),
]


class AnthropicContextManagement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edits: list[AnthropicContextEdit]


class AnthropicJsonOutputFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["json_schema"]
    schema_: JsonSchema = Field(alias="schema")


class AnthropicOutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    format: AnthropicJsonOutputFormat | None = None


class AnthropicToolChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["auto", "any", "tool", "none"]
    name: str | None = None
    disable_parallel_tool_use: bool | None = None

    @model_validator(mode="after")
    def require_tool_name(self) -> AnthropicToolChoice:
        if self.type == "tool" and not self.name:
            raise ValueError("tool_choice type 'tool' requires name")
        if self.type != "tool" and self.name is not None:
            raise ValueError("tool_choice name is only valid for type 'tool'")
        return self


class AnthropicToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    input_schema: JsonSchema = Field(default_factory=dict)


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[AnthropicMessage]
    system: AnthropicContent | None = None
    max_tokens: int = Field(ge=1)
    cache_control: AnthropicCacheControl | None = None
    container: str | None = None
    context_management: AnthropicContextManagement | None = None
    inference_geo: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=1)
    stream: bool = False
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0)
    stop_sequences: list[str] | None = None
    tools: list[AnthropicToolDefinition] | None = None
    tool_choice: AnthropicToolChoice | None = None
    metadata: AnthropicMetadata | None = None
    thinking: AnthropicThinking | None = None
    output_config: AnthropicOutputConfig | None = None
    service_tier: Literal["auto", "standard_only"] | None = None
    user_profile_id: str | None = None

    @model_validator(mode="after")
    def validate_conditional_fields(self) -> AnthropicMessagesRequest:
        if (
            isinstance(self.thinking, AnthropicThinkingEnabled)
            and self.tool_choice is not None
            and self.tool_choice.type in {"any", "tool"}
        ):
            raise ValueError("manual thinking only supports tool_choice types 'auto' and 'none'")
        if self.tool_choice is not None and self.tool_choice.type in {"any", "tool"} and not self.tools:
            raise ValueError(f"tool_choice type '{self.tool_choice.type}' requires tools")
        if self.tool_choice is not None and self.tool_choice.type == "tool":
            tool_names = {tool.name for tool in self.tools or []}
            if self.tool_choice.name not in tool_names:
                raise ValueError("tool_choice name must reference a supplied tool")
        return self


class CountTokensRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[AnthropicMessage]
    cache_control: AnthropicCacheControl | None = None
    context_management: AnthropicContextManagement | None = None
    output_config: AnthropicOutputConfig | None = None
    system: AnthropicContent | None = None
    thinking: AnthropicThinking | None = None
    tool_choice: AnthropicToolChoice | None = None
    tools: list[AnthropicToolDefinition] | None = None
    user_profile_id: str | None = None


@dataclass(frozen=True)
class _AnthropicGovernedLeaf:
    text: str
    replace: object


def _anthropic_governed_leaves(
    message: AnthropicMessage,
) -> list[_AnthropicGovernedLeaf]:
    content = message.content
    if isinstance(content, str):
        return [_AnthropicGovernedLeaf(content, message)]
    leaves: list[_AnthropicGovernedLeaf] = []
    for block in content:
        if isinstance(block, AnthropicTextBlock):
            leaves.append(_AnthropicGovernedLeaf(block.text, block))
        elif isinstance(block, AnthropicToolResultBlock):
            leaves.append(_AnthropicGovernedLeaf(_tool_result_content_str(block.content), block))
    return leaves


def _replace_anthropic_governed_leaf(leaf: _AnthropicGovernedLeaf, replacement: str) -> None:
    target = leaf.replace
    if isinstance(target, AnthropicMessage):
        target.content = replacement
    elif isinstance(target, AnthropicTextBlock):
        target.text = replacement
    else:
        assert isinstance(target, AnthropicToolResultBlock)
        _redact_tool_result_content(target, replacement)


@dataclass(frozen=True)
class AnthropicGatewayPayload:
    request: AnthropicMessagesRequest
    protocol: WireProtocol = "anthropic_messages"
    native_providers: frozenset[str] = frozenset({"anthropic"})

    @property
    def model(self) -> str:
        return self.request.model

    @property
    def stream(self) -> bool:
        return self.request.stream

    def governance_text(self) -> str:
        for message in reversed(self.request.messages):
            if message.role == "user":
                return "".join(leaf.text for leaf in _anthropic_governed_leaves(message))
        return ""

    def with_redacted_text(self, redacted_text: str) -> AnthropicGatewayPayload:
        request = self.request.model_copy(deep=True)
        for message in reversed(request.messages):
            if message.role != "user":
                continue
            leaves = _anthropic_governed_leaves(message)
            replacements = redistribute_redacted_text([leaf.text for leaf in leaves], redacted_text)
            # `request` above is already a deep copy, so these blocks are
            # fresh objects owned only by this payload.
            for leaf, replacement in zip(leaves, replacements, strict=True):
                _replace_anthropic_governed_leaf(leaf, replacement)
            break
        return AnthropicGatewayPayload(request=request)

    def native_body(self) -> JsonObject:
        return self.request.model_dump(mode="json", by_alias=True, exclude_none=True)

    def to_chat_body(self) -> JsonObject:
        return messages_to_chat_body(self.request)


def _tool_result_content_str(content: str | list[AnthropicToolResultContentBlock] | None) -> str:
    """Flatten a tool_result block's `content` to text, best-effort.

    Image blocks contribute no text here. Callers that must not silently drop
    an image (e.g. Chat translation, which cannot represent one in a tool-role
    message) check for it explicitly before calling this.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(block.text for block in content if isinstance(block, AnthropicTextBlock))


def _redact_tool_result_content(block: AnthropicToolResultBlock, redacted_text: str) -> None:
    """Write a tool_result block's share of the redacted text back into its
    content, in place.

    `block` is assumed to belong to an already deep-copied request tree (see
    AnthropicGatewayPayload.with_redacted_text), so mutating it here is safe.
    Mirrors _tool_result_content_str's flattening: image blocks contribute no
    text and are left untouched; a further redistribution is done across any
    nested text blocks so their individual boundaries are preserved too.

    There are two cases where there is no existing text block to write a
    non-empty share into: `content` is `None`, or `content` is a list that
    holds only non-text blocks (e.g. an image-only tool_result). Both must
    still preserve that share rather than silently drop it. `None` becomes a
    plain string — the simplest equivalent representation, and what this
    function already did before this fix. A non-text-only list instead gets
    the share appended as a new text block, so the existing (e.g. image)
    blocks are preserved rather than being clobbered by a string.
    """
    if isinstance(block.content, str):
        block.content = redacted_text
        return
    if block.content is None:
        if redacted_text:
            block.content = redacted_text
        return
    text_blocks = [sub_block for sub_block in block.content if isinstance(sub_block, AnthropicTextBlock)]
    if not text_blocks:
        if redacted_text:
            block.content = [*block.content, AnthropicTextBlock(text=redacted_text)]
        return
    replacements = redistribute_redacted_text([sub_block.text for sub_block in text_blocks], redacted_text)
    for sub_block, replacement in zip(text_blocks, replacements, strict=True):
        sub_block.text = replacement


def _content_str(content: AnthropicContent) -> str:
    """Best-effort flattening of Anthropic message/system content to plain text.

    Used for the system prompt and for token-count approximation, both of which only
    need a representative string rather than a structural translation. tool_use/
    tool_result blocks are approximated by their JSON/text payload rather than
    rejected, so count_tokens keeps working on conversations that include them.
    """
    if isinstance(content, str):
        return content

    fragments: list[str] = []
    for block in content:
        if isinstance(block, AnthropicTextBlock):
            fragments.append(block.text)
        elif isinstance(block, AnthropicToolUseBlock):
            fragments.append(json.dumps(block.input))
        elif isinstance(block, AnthropicToolResultBlock):
            fragments.append(_tool_result_content_str(block.content))
        else:
            raise AnthropicCompatError(f"Unsupported content block type: {block.type}")
    return "".join(fragments)


def _tool_result_to_message(block: AnthropicToolResultBlock, index: int) -> JsonObject:
    """Translate a representable Anthropic tool_result to an OpenAI tool message."""
    if block.is_error:
        raise AnthropicCompatError(
            f"tool_result block at index {index} has is_error=true, which Chat cannot preserve"
        )
    if isinstance(block.content, list) and any(
        isinstance(item, AnthropicImageBlock) for item in block.content
    ):
        raise AnthropicCompatError(
            f"tool_result block at index {index} contains image content, "
            "which Chat cannot represent in a tool message"
        )
    text = _tool_result_content_str(block.content)
    return {"role": "tool", "tool_call_id": block.tool_use_id, "content": text}


def _expand_user_content_blocks(
    content: list[AnthropicContentBlock], index: int
) -> list[JsonObject]:
    """Translate a user message's content blocks into one or more OpenAI messages.

    Contiguous text blocks become a single user message; each tool_result block
    becomes its own role:"tool" message, in the same order they appear so a
    conversation with interleaved text/tool_result blocks preserves ordering.
    """
    messages: list[JsonObject] = []
    text_buffer: list[str] = []

    def _flush_text() -> None:
        if text_buffer:
            messages.append({"role": "user", "content": "".join(text_buffer)})
            text_buffer.clear()

    for block in content:
        if isinstance(block, AnthropicTextBlock):
            text_buffer.append(block.text)
        elif isinstance(block, AnthropicToolResultBlock):
            _flush_text()
            messages.append(_tool_result_to_message(block, index))
        else:
            raise AnthropicCompatError(f"Unsupported content block type: {block.type}")

    _flush_text()
    if not messages:
        messages.append({"role": "user", "content": ""})
    return messages


def _assistant_content_to_message(content: list[AnthropicContentBlock], index: int) -> JsonObject:
    """Translate an assistant message's content blocks (text + tool_use) to OpenAI shape."""
    text_fragments: list[str] = []
    tool_calls: list[JsonObject] = []

    for block in content:
        if isinstance(block, AnthropicTextBlock):
            text_fragments.append(block.text)
        elif isinstance(block, AnthropicToolUseBlock):
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": json.dumps(block.input)},
                }
            )
        else:
            raise AnthropicCompatError(f"Unsupported content block type: {block.type}")

    message: JsonObject = {"role": "assistant", "content": "".join(text_fragments)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _tools_to_openai(tools: list[AnthropicToolDefinition]) -> list[JsonObject]:
    """Translate Anthropic tool definitions to OpenAI function-tool schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]


def _tool_choice_to_openai(tool_choice: AnthropicToolChoice) -> str | JsonObject:
    """Translate Anthropic tool_choice to its OpenAI chat-completions equivalent.

    Anthropic shapes: {"type": "auto"}, {"type": "any"}, {"type": "none"},
    {"type": "tool", "name": "X"}.
    """
    choice_type = tool_choice.type
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        name = tool_choice.name
        if not name:
            raise AnthropicCompatError("tool_choice of type 'tool' must include a name")
        return {"type": "function", "function": {"name": name}}
    raise AnthropicCompatError(f"Unsupported tool_choice type: {choice_type}")


def messages_to_chat_body(req: AnthropicMessagesRequest) -> JsonObject:
    """Translate Anthropic Messages request to internal chat completion body."""
    msgs: list[JsonObject] = []
    if req.system:
        msgs.append({"role": "system", "content": _content_str(req.system)})
    for index, msg in enumerate(req.messages):
        role = msg.role.lower()
        if role not in {"user", "assistant", "system"}:
            raise AnthropicCompatError(f"Unsupported message role at index {index}: {msg.role}")
        content = msg.content
        if role == "system":
            msgs.append({"role": "system", "content": _content_str(content)})
        elif isinstance(content, str):
            msgs.append({"role": role, "content": content})
        elif role == "assistant":
            msgs.append(_assistant_content_to_message(content, index))
        else:
            msgs.extend(_expand_user_content_blocks(content, index))

    body: JsonObject = {
        "model": req.model,
        "messages": msgs,
        "max_tokens": req.max_tokens,
        "stream": req.stream,
    }
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.top_p is not None:
        body["top_p"] = req.top_p
    if req.top_k is not None:
        body["top_k"] = req.top_k
    if req.stop_sequences:
        body["stop"] = req.stop_sequences
    if req.tools:
        body["tools"] = _tools_to_openai(req.tools)
    if req.tool_choice is not None:
        body["tool_choice"] = _tool_choice_to_openai(req.tool_choice)
        if req.tool_choice.disable_parallel_tool_use:
            body["parallel_tool_calls"] = False
    native_only_fields = (
        "cache_control",
        "container",
        "context_management",
        "inference_geo",
        "metadata",
        "output_config",
        "service_tier",
        "thinking",
        "user_profile_id",
    )
    unsupported = [key for key in native_only_fields if getattr(req, key) is not None]
    if unsupported:
        raise AnthropicCompatError(
            "Anthropic fields cannot be translated to Chat without loss: "
            + ", ".join(unsupported)
        )
    return body


_FINISH_TO_STOP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}


def _tool_calls_to_blocks(tool_calls: list[JsonObject]) -> list[JsonObject]:
    blocks: list[JsonObject] = []
    for call in tool_calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            continue
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError:
            arguments = {"arguments": raw_arguments}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or "toolu_compat",
                "name": function.get("name") or "unknown_tool",
                "input": arguments if isinstance(arguments, dict) else {"value": arguments},
            }
        )
    return blocks


def chat_response_to_anthropic(chat_json: JsonObject, model: str) -> JsonObject:
    """Translate OpenAI chat completion JSON to Anthropic Messages API response shape."""
    try:
        chat_response = OpenAIChatResponse.model_validate(chat_json)
    except ValidationError as exc:
        raise AnthropicCompatError("Chat response does not match the supported envelope") from exc
    if not chat_response.choices:
        raise AnthropicCompatError("Chat response must include at least one choice")
    choice = chat_response.choices[0]
    message = choice.message
    stop_reason = _FINISH_TO_STOP.get(choice.finish_reason or "stop", "end_turn")

    content: list[JsonObject] = []
    message_content = message.content
    if isinstance(message_content, str) and message_content:
        content.append({"type": "text", "text": message_content})
    elif isinstance(message_content, list):
        for block in message_content:
            if block.get("type") != "text" or not isinstance(block.get("text"), str):
                raise AnthropicCompatError(
                    "Only assistant text content can be translated to Anthropic Messages"
                )
            content.append({"type": "text", "text": block["text"]})
    if message.tool_calls:
        content.extend(_tool_calls_to_blocks(message.tool_calls))
    if not content:
        content.append({"type": "text", "text": ""})

    return {
        "id": chat_response.id,
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": chat_response.model or model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": chat_response.usage.prompt_tokens,
            "output_tokens": chat_response.usage.completion_tokens,
        },
    }


def _message_start_event(message_id: str, model: str) -> str:
    _start = {
        "type": "message_start",
        "message": {
            "id": message_id, "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    return f"event: message_start\ndata: {json.dumps(_start)}\n\n"


def _content_block_start_text_event() -> str:
    _cb_start = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    return f"event: content_block_start\ndata: {json.dumps(_cb_start)}\n\n"


def _content_block_stop_event(index: int) -> str:
    return f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': index})}\n\n"


def _message_delta_event(stop_reason: str, output_tokens: int) -> str:
    _msg_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }
    return f"event: message_delta\ndata: {json.dumps(_msg_delta)}\n\n"


def _message_stop_event() -> str:
    return f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"


async def openai_sse_to_anthropic_sse(body_iterator, model: str):
    """Translate OpenAI SSE stream chunks to Anthropic Messages SSE events.

    Text deltas stream on content block index 0 (opened eagerly on the first event,
    whose id is reused as the message id so the client sees the real provider-assigned
    id instead of a fabricated placeholder). Tool-call deltas are tracked per OpenAI
    `tool_calls[].index` and translated into their own Anthropic content blocks
    (index 1, 2, ...): a content_block_start carrying the tool id/name the first time
    an index is seen, then input_json_delta events for each `arguments` fragment, with
    all open blocks closed out once a finish_reason arrives. A bare `{"error": ...}`
    event (emitted by provider adapters on upstream timeout/connection failure) is
    surfaced as an Anthropic `error` SSE event instead of being silently dropped.
    """
    message_id = ""
    message_started = False
    open_block_indices: list[int] = []
    tool_index_by_openai_index: dict[int, int] = {}
    next_block_index = 1  # index 0 is reserved for the text block
    output_tokens = 0
    finish_reason: str | None = None

    async for event in iter_openai_chat_events(body_iterator):
        if isinstance(event, dict):
            raw_error = event.get("error")
            error_info = raw_error if isinstance(raw_error, dict) else {"message": str(raw_error)}
            _error = {
                "type": "error",
                "error": {
                    "type": error_info.get("type", "api_error"),
                    "message": error_info.get("message", "Upstream provider error"),
                },
            }
            yield f"event: error\ndata: {json.dumps(_error)}\n\n"
            return

        if not message_started:
            message_id = event.id
            yield _message_start_event(message_id, model)
            yield _content_block_start_text_event()
            open_block_indices.append(0)
            message_started = True

        if event.usage is not None:
            output_tokens = event.usage.completion_tokens

        if not event.choices:
            continue
        if finish_reason is not None:
            _error = {
                "type": "error",
                "error": {
                    "type": "invalid_upstream_stream",
                    "message": "Provider emitted content after a terminal finish reason",
                },
            }
            yield f"event: error\ndata: {json.dumps(_error)}\n\n"
            return
        choice = event.choices[0]
        delta = choice.delta
        chunk_finish_reason = choice.finish_reason
        if chunk_finish_reason is not None:
            finish_reason = chunk_finish_reason

        if delta.content:
            _delta = {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": delta.content},
            }
            yield f"event: content_block_delta\ndata: {json.dumps(_delta)}\n\n"

        tool_call_deltas = delta.tool_calls
        if tool_call_deltas is not None:
            for tool_call in tool_call_deltas:
                openai_index = tool_call.index
                function = tool_call.function
                if openai_index not in tool_index_by_openai_index:
                    if not tool_call.id or function is None or not function.name:
                        _error = {
                            "type": "error",
                            "error": {
                                "type": "invalid_upstream_stream",
                                "message": "Initial tool-call chunk is missing id or function name",
                            },
                        }
                        yield f"event: error\ndata: {json.dumps(_error)}\n\n"
                        return
                    block_index = next_block_index
                    next_block_index += 1
                    tool_index_by_openai_index[openai_index] = block_index
                    _cb_start = {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": function.name,
                            "input": {},
                        },
                    }
                    yield f"event: content_block_start\ndata: {json.dumps(_cb_start)}\n\n"
                    open_block_indices.append(block_index)
                else:
                    block_index = tool_index_by_openai_index[openai_index]

                arguments_fragment = function.arguments if function is not None else None
                if arguments_fragment:
                    _delta = {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "input_json_delta", "partial_json": arguments_fragment},
                    }
                    yield f"event: content_block_delta\ndata: {json.dumps(_delta)}\n\n"

    if finish_reason is not None:
        stop_reason = _FINISH_TO_STOP.get(finish_reason, "end_turn")
        for index in open_block_indices:
            yield _content_block_stop_event(index)
        yield _message_delta_event(stop_reason, output_tokens)
        yield _message_stop_event()
        return

    _error = {
        "type": "error",
        "error": {
            "type": "incomplete_upstream_stream",
            "message": "Provider stream ended without a terminal finish reason",
        },
    }
    yield f"event: error\ndata: {json.dumps(_error)}\n\n"


def count_tokens_approximate(
    messages: list[AnthropicMessage], system: AnthropicContent | None = None
) -> int:
    """Deterministic token approximation (~4 chars per token, plus per-message overhead)."""
    total_chars = len(_content_str(system)) if system else 0
    for msg in messages:
        total_chars += len(_content_str(msg.content))
    return max(1, (total_chars + 3) // 4 + len(messages) * 5)
