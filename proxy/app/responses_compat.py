from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

from proxy.app.anthropic_compat import _iter_openai_chat_events
from proxy.app.protocol_types import (
    ExecutionFunctionCallItem,
    ExecutionFunctionCallOutputItem,
    ExecutionItemReference,
    ExecutionReasoningItem,
    JsonObject,
    OpenAIChatResponse,
    ProtocolTranslationError,
    WireProtocol,
    redistribute_redacted_text,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator


class ResponsesCompatError(ProtocolTranslationError):
    pass


class ResponsesTextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    text: str | None = None


class ResponsesInputMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str | list[ResponsesTextPart]
    type: str | None = None


# `ResponsesInputMessage.content` parts deliberately keep `type` as a plain `str`
# (not a Literal-discriminated part union) rather than reusing
# `protocol_types.ExecutionInputContent`: test_responses_rejects_unsupported_shape
# pins an ingress-accepts / translation-rejects contract (an unsupported part type
# like "input_image" must pass model_validate() and only fail once
# translate_responses_request() inspects it, so the caller gets HTTP 422
# unsupported_response_shape rather than HTTP 400 invalid_request). The
# agent-lifecycle item types below have no such constraint, so they reuse the
# canonical Execution*Item models directly.
ResponsesInputItem = (
    ResponsesInputMessage
    | ExecutionFunctionCallItem
    | ExecutionFunctionCallOutputItem
    | ExecutionReasoningItem
    | ExecutionItemReference
)


class ResponsesReasoning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: Literal["auto", "current_turn", "all_turns"] | None = None
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    generate_summary: Literal["auto", "concise", "detailed"] | None = None
    mode: str | None = None
    summary: Literal["auto", "concise", "detailed"] | None = None


class ResponsesTextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: JsonObject | None = None
    verbosity: Literal["low", "medium", "high"] | None = None


class ResponsesCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    input: str | list[ResponsesInputItem]
    instructions: str | list[ResponsesTextPart] | None = None
    background: bool = False
    client_metadata: dict[str, str] | None = None
    context_management: list[JsonObject] | None = None
    conversation: str | JsonObject | None = None
    include: list[str] | None = None
    stream: bool = False
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    temperature: float | None = None
    top_p: float | None = None
    tools: list[JsonObject] | None = None
    tool_choice: JsonObject | str | None = None
    previous_response_id: str | None = None
    metadata: dict[str, str] | None = None
    moderation: JsonObject | None = None
    parallel_tool_calls: bool | None = None
    prompt: JsonObject | None = None
    prompt_cache_key: str | None = None
    prompt_cache_options: JsonObject | None = None
    prompt_cache_retention: Literal["in-memory", "24h"] | None = None
    reasoning: ResponsesReasoning | None = None
    safety_identifier: str | None = None
    service_tier: str | None = None
    store: bool | None = None
    stream_options: JsonObject | None = None
    text: ResponsesTextConfig | None = None
    top_logprobs: int | None = Field(default=None, ge=0, le=20)
    truncation: Literal["auto", "disabled"] | None = None
    user: str | None = None

    @model_validator(mode="after")
    def validate_lifecycle_fields(self) -> ResponsesCreateRequest:
        if self.previous_response_id is not None and self.conversation is not None:
            raise ValueError("previous_response_id and conversation are mutually exclusive")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        return self


def _responses_input_text(input_value: str | list[ResponsesInputItem]) -> str:
    if isinstance(input_value, str):
        return input_value
    for item in reversed(input_value):
        if not isinstance(item, ResponsesInputMessage) or item.role != "user":
            continue
        content = item.content
        if isinstance(content, str):
            return content
        return "".join(
            part.text or "" for part in content if part.type in {"input_text", "text"}
        )
    return ""


@dataclass(frozen=True)
class ResponsesGatewayPayload:
    request: ResponsesCreateRequest
    protocol: WireProtocol = "openai_responses"
    native_providers: frozenset[str] = frozenset({"openai"})

    @property
    def model(self) -> str:
        return self.request.model

    @property
    def stream(self) -> bool:
        return self.request.stream

    def governance_text(self) -> str:
        return _responses_input_text(self.request.input)

    def with_redacted_text(self, redacted_text: str) -> ResponsesGatewayPayload:
        if isinstance(self.request.input, str):
            request = self.request.model_copy(update={"input": redacted_text})
            return ResponsesGatewayPayload(request=request)

        new_input = list(self.request.input)
        for position in range(len(new_input) - 1, -1, -1):
            item = new_input[position]
            if not isinstance(item, ResponsesInputMessage) or item.role != "user":
                continue
            if isinstance(item.content, str):
                new_input[position] = item.model_copy(update={"content": redacted_text})
            else:
                text_parts = [
                    part for part in item.content if part.type in {"input_text", "text"}
                ]
                replacements = redistribute_redacted_text(
                    [part.text or "" for part in text_parts], redacted_text
                )
                replaced = iter(
                    part.model_copy(update={"text": replacement})
                    for part, replacement in zip(text_parts, replacements, strict=True)
                )
                new_input[position] = item.model_copy(
                    update={
                        "content": [
                            next(replaced) if part.type in {"input_text", "text"} else part
                            for part in item.content
                        ]
                    }
                )
            break
        request = self.request.model_copy(update={"input": new_input})
        return ResponsesGatewayPayload(request=request)

    def native_body(self) -> JsonObject:
        return self.request.model_dump(mode="json", exclude_none=True, exclude_unset=True)

    def to_chat_body(self) -> JsonObject:
        return _request_to_chat_body(self.request)


class ResponsesOutputText(BaseModel):
    type: Literal["output_text"] = "output_text"
    text: str
    annotations: list[JsonObject] = Field(default_factory=list)


class ResponsesOutputMessage(BaseModel):
    id: str
    type: Literal["message"] = "message"
    status: Literal["completed", "incomplete"] = "completed"
    role: Literal["assistant"] = "assistant"
    content: list[ResponsesOutputText]


class ResponsesFunctionCall(BaseModel):
    id: str
    type: Literal["function_call"] = "function_call"
    status: Literal["in_progress", "completed", "incomplete"] = "completed"
    call_id: str
    name: str
    arguments: str


class ResponsesUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_tokens_details: dict[str, int] = Field(default_factory=lambda: {"cached_tokens": 0})
    output_tokens_details: dict[str, int] = Field(default_factory=lambda: {"reasoning_tokens": 0})


class ResponsesIncompleteDetails(BaseModel):
    reason: str


class ResponsesCreateResponse(BaseModel):
    id: str
    object: Literal["response"] = "response"
    created_at: int
    status: Literal["completed", "incomplete"] = "completed"
    model: str
    output: list[ResponsesOutputMessage | ResponsesFunctionCall]
    output_text: str = ""
    error: None = None
    incomplete_details: ResponsesIncompleteDetails | None = None
    usage: ResponsesUsage = Field(default_factory=ResponsesUsage)


def _status_from_finish_reason(
    finish_reason: str | None,
) -> tuple[Literal["completed", "incomplete"], ResponsesIncompleteDetails | None]:
    """Map an OpenAI chat-completions finish_reason to a truthful Responses status.

    Only ``length`` (max output tokens hit) currently downgrades the response to
    ``incomplete`` — every other finish_reason (including tool_calls, which this
    compat layer does not yet emit output for) is reported as ``completed``.
    """
    if finish_reason == "length":
        return "incomplete", ResponsesIncompleteDetails(reason="max_output_tokens")
    return "completed", None


def _parts_to_text(parts: list[ResponsesTextPart], *, field_name: str) -> str:
    text_fragments: list[str] = []
    for part in parts:
        if part.type not in {"input_text", "text"}:
            raise ResponsesCompatError(
                f"Unsupported {field_name} content part type: {part.type}"
            )
        if part.text is None:
            raise ResponsesCompatError(f"{field_name} text parts must include text")
        text_fragments.append(part.text)
    return "".join(text_fragments)


def _content_to_text(
    content: str | list[ResponsesTextPart],
    *,
    field_name: str,
) -> str:
    if isinstance(content, str):
        return content
    return _parts_to_text(content, field_name=field_name)


def translate_responses_request(payload: JsonObject) -> JsonObject:
    try:
        req = ResponsesCreateRequest.model_validate(payload)
    except ValidationError as exc:
        raise ResponsesCompatError(
            "Request body does not match the supported OpenAI Responses subset"
        ) from exc
    return _request_to_chat_body(req)


def _request_to_chat_body(req: ResponsesCreateRequest) -> JsonObject:
    """Translate an already-typed ResponsesCreateRequest to a chat body.

    Split out of translate_responses_request() so ResponsesGatewayPayload.
    to_chat_body() can operate on the already-typed request it holds
    directly (mirroring anthropic_compat.py's messages_to_chat_body()
    pattern) instead of round-tripping through model_dump() +
    model_validate() again.
    """
    if req.tools:
        raise ResponsesCompatError("Responses tools are not supported yet")
    if req.tool_choice is not None:
        raise ResponsesCompatError("Responses tool_choice is not supported yet")
    if req.previous_response_id is not None:
        raise ResponsesCompatError("previous_response_id is not supported yet")
    unsupported_fields = {
        "background": req.background or None,
        "client_metadata": req.client_metadata,
        "context_management": req.context_management,
        "conversation": req.conversation,
        "include": req.include,
        "max_tool_calls": req.max_tool_calls,
        "moderation": req.moderation,
        "parallel_tool_calls": req.parallel_tool_calls,
        "prompt": req.prompt,
        "prompt_cache_key": req.prompt_cache_key,
        "prompt_cache_options": req.prompt_cache_options,
        "prompt_cache_retention": req.prompt_cache_retention,
        "reasoning": req.reasoning,
        "safety_identifier": req.safety_identifier,
        "service_tier": req.service_tier,
        "store": req.store,
        "stream_options": req.stream_options,
        "text": req.text,
        "top_logprobs": req.top_logprobs,
        "truncation": req.truncation,
        "user": req.user,
    }
    present_unsupported = [name for name, value in unsupported_fields.items() if value is not None]
    if present_unsupported:
        fields = ", ".join(present_unsupported)
        raise ResponsesCompatError(f"Responses fields are not supported by chat translation: {fields}")

    messages: list[JsonObject] = []

    if req.instructions is not None:
        instructions_text = _content_to_text(req.instructions, field_name="instructions")
        messages.append({"role": "system", "content": instructions_text})

    if isinstance(req.input, str):
        messages.append({"role": "user", "content": req.input})
    else:
        for index, item in enumerate(req.input):
            if not isinstance(item, ResponsesInputMessage):
                raise ResponsesCompatError(
                    f"Unsupported input item type at index {index}: {item.type}"
                )
            item_type = (item.type or "message").lower()
            if item_type != "message":
                raise ResponsesCompatError(
                    f"Unsupported input item type at index {index}: {item_type}"
                )
            role = item.role.lower()
            if role == "developer":
                role = "system"
            if role not in {"system", "user", "assistant"}:
                raise ResponsesCompatError(
                    f"Unsupported input role at index {index}: {item.role}"
                )
            messages.append(
                {
                    "role": role,
                    "content": _content_to_text(
                        item.content, field_name=f"input[{index}].content"
                    ),
                }
            )

    if not messages:
        raise ResponsesCompatError("Responses requests must include at least one message")

    body: JsonObject = {
        "model": req.model,
        "messages": messages,
        "stream": req.stream,
    }
    if req.max_output_tokens is not None:
        body["max_tokens"] = req.max_output_tokens
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.top_p is not None:
        body["top_p"] = req.top_p
    return body


def _assistant_text_from_chat_response(response: OpenAIChatResponse) -> str:
    for choice in response.choices:
        content = choice.message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            fragments: list[str] = []
            for part in content:
                text = part.get("text")
                if isinstance(text, str):
                    fragments.append(text)
            return "".join(fragments)
    return ""


def translate_chat_response(body: JsonObject) -> JsonObject:
    try:
        chat_response = OpenAIChatResponse.model_validate(body)
    except ValidationError as exc:
        raise ResponsesCompatError("Chat response does not match the supported envelope") from exc
    chat_id = chat_response.id
    response_id = chat_id if chat_id.startswith("resp_") else f"resp_{chat_id}"
    message_id = response_id.replace("resp_", "msg_", 1)
    output_text = _assistant_text_from_chat_response(chat_response)
    finish_reason = chat_response.choices[0].finish_reason if chat_response.choices else None
    status, incomplete_details = _status_from_finish_reason(finish_reason)
    usage = chat_response.usage
    output: list[ResponsesOutputMessage | ResponsesFunctionCall] = []
    if output_text or not chat_response.choices or not chat_response.choices[0].message.tool_calls:
        output.append(
            ResponsesOutputMessage(
                id=message_id,
                status=status,
                content=[ResponsesOutputText(text=output_text)],
            )
        )
    if chat_response.choices:
        for index, tool_call in enumerate(chat_response.choices[0].message.tool_calls or []):
            function = tool_call.get("function")
            if not isinstance(function, dict):
                raise ResponsesCompatError(f"Chat tool call {index} is missing function")
            call_id = tool_call.get("id")
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(call_id, str) or not call_id:
                raise ResponsesCompatError(f"Chat tool call {index} has invalid identity")
            if not isinstance(name, str) or not name:
                raise ResponsesCompatError(f"Chat tool call {index} has invalid identity")
            if not isinstance(arguments, str):
                raise ResponsesCompatError(f"Chat tool call {index} has invalid arguments")
            output.append(
                ResponsesFunctionCall(
                    id=f"fc_{call_id}",
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    status=status,
                )
            )
    response = ResponsesCreateResponse(
        id=response_id,
        created_at=int(chat_response.created),
        status=status,
        model=chat_response.model,
        output=output,
        output_text=output_text,
        incomplete_details=incomplete_details,
        usage=ResponsesUsage(
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
        ),
    )
    return cast(JsonObject, response.model_dump(mode="json"))


def _sse_frame(event_type: str, **fields: JsonValue) -> str:
    payload: JsonObject = {"type": event_type, **fields}
    return f"data: {json.dumps(payload)}\n\n"


@dataclass
class _StreamingFunctionCall:
    item_id: str
    call_id: str
    name: str
    output_index: int
    arguments: str = ""


def _failed_stream_frame(
    *,
    response_id: str,
    created_at: int,
    model: str,
    message_id: str,
    text_fragments: list[str],
    text_output_index: int | None,
    function_calls: dict[int, _StreamingFunctionCall],
    usage: ResponsesUsage,
    error_type: str,
    error_message: str,
) -> str:
    text = "".join(text_fragments)
    output: list[JsonObject] = []
    if text_output_index is not None:
        output.append(
            {
                "id": message_id,
                "type": "message",
                "status": "incomplete",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    for tool_index in sorted(function_calls):
        state = function_calls[tool_index]
        output.append(
            {
                "id": state.item_id,
                "type": "function_call",
                "status": "incomplete",
                "call_id": state.call_id,
                "name": state.name,
                "arguments": state.arguments,
            }
        )
    return _sse_frame(
        "response.failed",
        response={
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": "failed",
            "model": model,
            "output": output,
            "output_text": text,
            "error": {"type": error_type, "message": error_message},
            "incomplete_details": None,
            "usage": usage.model_dump(),
        },
    )


async def openai_sse_to_responses_sse(body_iterator, model: str):
    """Translate Chat SSE into a loss-aware Responses text/tool lifecycle."""
    response_id = f"resp_{uuid4().hex}"
    message_id = f"msg_{uuid4().hex}"
    created_at = int(datetime.now(UTC).timestamp())
    next_output_index = 0
    content_index = 0
    final_model = model
    finish_reason: str | None = None
    text_fragments: list[str] = []
    text_output_index: int | None = None
    function_calls: dict[int, _StreamingFunctionCall] = {}
    final_usage = ResponsesUsage()

    yield _sse_frame(
        "response.created",
        response={
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": "in_progress",
            "model": model,
            "output": [],
            "output_text": "",
            "error": None,
            "incomplete_details": None,
            "usage": None,
        },
    )

    async for decoded_event in _iter_openai_chat_events(body_iterator):
        if isinstance(decoded_event, dict):
            raw_error = decoded_event.get("error")
            error_info = raw_error if isinstance(raw_error, dict) else {}
            error_type = error_info.get("type")
            error_message = error_info.get("message")
            yield _failed_stream_frame(
                response_id=response_id,
                created_at=created_at,
                model=final_model,
                message_id=message_id,
                text_fragments=text_fragments,
                text_output_index=text_output_index,
                function_calls=function_calls,
                usage=final_usage,
                error_type=(
                    error_type if isinstance(error_type, str) else "provider_stream_error"
                ),
                error_message=(
                    error_message if isinstance(error_message, str) else "Provider stream failed"
                ),
            )
            return

        event = decoded_event.model_dump(mode="json", exclude_unset=True)
        event_model = event.get("model")
        if isinstance(event_model, str):
            final_model = event_model

        raw_usage = event.get("usage")
        if isinstance(raw_usage, dict):
            prompt_tokens = raw_usage.get("prompt_tokens", 0)
            completion_tokens = raw_usage.get("completion_tokens", 0)
            total_tokens = raw_usage.get("total_tokens", 0)
            final_usage = ResponsesUsage(
                input_tokens=prompt_tokens if isinstance(prompt_tokens, int) else 0,
                output_tokens=completion_tokens if isinstance(completion_tokens, int) else 0,
                total_tokens=total_tokens if isinstance(total_tokens, int) else 0,
            )

        raw_error = event.get("error")
        if isinstance(raw_error, dict):
            error_type = raw_error.get("type")
            error_message = raw_error.get("message")
            yield _failed_stream_frame(
                response_id=response_id,
                created_at=created_at,
                model=final_model,
                message_id=message_id,
                text_fragments=text_fragments,
                text_output_index=text_output_index,
                function_calls=function_calls,
                usage=final_usage,
                error_type=(
                    error_type if isinstance(error_type, str) else "provider_stream_error"
                ),
                error_message=(
                    error_message if isinstance(error_message, str) else "Provider stream failed"
                ),
            )
            return

        choices = event.get("choices") or []
        if not choices:
            continue
        if not isinstance(choices, list) or not isinstance(choices[0], dict):
            raise ResponsesCompatError("Chat stream choices must contain objects")
        choice = choices[0]
        raw_delta = choice.get("delta", {})
        if not isinstance(raw_delta, dict):
            raise ResponsesCompatError("Chat stream delta must be an object")
        delta = raw_delta
        chunk_finish_reason = choice.get("finish_reason")
        if isinstance(chunk_finish_reason, str):
            finish_reason = chunk_finish_reason

        text_delta = delta.get("content")
        if isinstance(text_delta, str) and text_delta:
            if text_output_index is None:
                text_output_index = next_output_index
                next_output_index += 1
                yield _sse_frame(
                    "response.output_item.added",
                    output_index=text_output_index,
                    item={
                        "id": message_id,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                )
                yield _sse_frame(
                    "response.content_part.added",
                    item_id=message_id,
                    output_index=text_output_index,
                    content_index=content_index,
                    part={"type": "output_text", "text": "", "annotations": []},
                )
            text_fragments.append(text_delta)
            yield _sse_frame(
                "response.output_text.delta",
                item_id=message_id,
                output_index=text_output_index,
                content_index=content_index,
                delta=text_delta,
            )

        raw_tool_calls = delta.get("tool_calls", [])
        if raw_tool_calls is not None:
            if not isinstance(raw_tool_calls, list):
                raise ResponsesCompatError("Chat stream tool_calls must be a list")
            for raw_tool_call in raw_tool_calls:
                if not isinstance(raw_tool_call, dict):
                    raise ResponsesCompatError("Chat stream tool_call must be an object")
                tool_index = raw_tool_call.get("index", 0)
                if not isinstance(tool_index, int):
                    raise ResponsesCompatError("Chat stream tool_call index must be an integer")
                raw_function = raw_tool_call.get("function", {})
                if not isinstance(raw_function, dict):
                    raise ResponsesCompatError("Chat stream tool_call function must be an object")
                state = function_calls.get(tool_index)
                call_id_delta = raw_tool_call.get("id")
                name_delta = raw_function.get("name")
                if state is None:
                    if not isinstance(call_id_delta, str) or not call_id_delta:
                        yield _failed_stream_frame(
                            response_id=response_id,
                            created_at=created_at,
                            model=final_model,
                            message_id=message_id,
                            text_fragments=text_fragments,
                            text_output_index=text_output_index,
                            function_calls=function_calls,
                            usage=final_usage,
                            error_type="invalid_upstream_stream",
                            error_message="Initial tool-call chunk is missing call id",
                        )
                        return
                    if not isinstance(name_delta, str) or not name_delta:
                        yield _failed_stream_frame(
                            response_id=response_id,
                            created_at=created_at,
                            model=final_model,
                            message_id=message_id,
                            text_fragments=text_fragments,
                            text_output_index=text_output_index,
                            function_calls=function_calls,
                            usage=final_usage,
                            error_type="invalid_upstream_stream",
                            error_message="Initial tool-call chunk is missing function name",
                        )
                        return
                    state = _StreamingFunctionCall(
                        item_id=f"fc_{uuid4().hex}",
                        call_id=call_id_delta,
                        name=name_delta,
                        output_index=next_output_index,
                    )
                    next_output_index += 1
                    function_calls[tool_index] = state
                    yield _sse_frame(
                        "response.output_item.added",
                        output_index=state.output_index,
                        item={
                            "id": state.item_id,
                            "type": "function_call",
                            "status": "in_progress",
                            "call_id": state.call_id,
                            "name": state.name,
                            "arguments": "",
                        },
                    )
                else:
                    if isinstance(call_id_delta, str) and call_id_delta != state.call_id:
                        yield _failed_stream_frame(
                            response_id=response_id,
                            created_at=created_at,
                            model=final_model,
                            message_id=message_id,
                            text_fragments=text_fragments,
                            text_output_index=text_output_index,
                            function_calls=function_calls,
                            usage=final_usage,
                            error_type="invalid_upstream_stream",
                            error_message="Tool-call identity changed during streaming",
                        )
                        return
                    if isinstance(name_delta, str):
                        state.name += name_delta
                arguments_delta = raw_function.get("arguments")
                if isinstance(arguments_delta, str) and arguments_delta:
                    state.arguments += arguments_delta
                    yield _sse_frame(
                        "response.function_call_arguments.delta",
                        item_id=state.item_id,
                        output_index=state.output_index,
                        delta=arguments_delta,
                    )

    if finish_reason is None:
        yield _failed_stream_frame(
            response_id=response_id,
            created_at=created_at,
            model=final_model,
            message_id=message_id,
            text_fragments=text_fragments,
            text_output_index=text_output_index,
            function_calls=function_calls,
            usage=final_usage,
            error_type="incomplete_upstream_stream",
            error_message="Provider stream ended without a terminal finish reason",
        )
        return

    final_text = "".join(text_fragments)
    status, incomplete_details = _status_from_finish_reason(finish_reason)
    final_output: list[ResponsesOutputMessage | ResponsesFunctionCall] = []

    if text_output_index is not None:
        yield _sse_frame(
            "response.output_text.done",
            item_id=message_id,
            output_index=text_output_index,
            content_index=content_index,
            text=final_text,
        )
        final_output_text = ResponsesOutputText(text=final_text)
        yield _sse_frame(
            "response.content_part.done",
            item_id=message_id,
            output_index=text_output_index,
            content_index=content_index,
            part=final_output_text.model_dump(),
        )
        final_message = ResponsesOutputMessage(
            id=message_id, status=status, content=[final_output_text]
        )
        final_output.append(final_message)
        yield _sse_frame(
            "response.output_item.done",
            output_index=text_output_index,
            item=final_message.model_dump(),
        )

    for tool_index in sorted(function_calls):
        state = function_calls[tool_index]
        if not state.name:
            raise ResponsesCompatError(f"Chat stream tool_call {tool_index} is missing a name")
        yield _sse_frame(
            "response.function_call_arguments.done",
            item_id=state.item_id,
            output_index=state.output_index,
            arguments=state.arguments,
        )
        final_call = ResponsesFunctionCall(
            id=state.item_id,
            call_id=state.call_id,
            name=state.name,
            arguments=state.arguments,
            status=status,
        )
        final_output.append(final_call)
        yield _sse_frame(
            "response.output_item.done",
            output_index=state.output_index,
            item=final_call.model_dump(),
        )

    final_response = ResponsesCreateResponse(
        id=response_id,
        created_at=created_at,
        status=status,
        model=final_model,
        output=final_output,
        output_text=final_text,
        incomplete_details=incomplete_details,
        usage=final_usage,
    )
    yield _sse_frame("response.completed", response=final_response.model_dump())
