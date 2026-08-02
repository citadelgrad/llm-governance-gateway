from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from proxy.app.anthropic_compat import _iter_sse_json


class ResponsesCompatError(Exception):
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


class ResponsesCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    input: str | list[ResponsesInputMessage]
    instructions: str | list[ResponsesTextPart] | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | str | None = None
    previous_response_id: str | None = None
    metadata: dict[str, Any] | None = None


class ResponsesOutputText(BaseModel):
    type: Literal["output_text"] = "output_text"
    text: str
    annotations: list[dict] = Field(default_factory=list)


class ResponsesOutputMessage(BaseModel):
    id: str
    type: Literal["message"] = "message"
    status: Literal["completed", "incomplete"] = "completed"
    role: Literal["assistant"] = "assistant"
    content: list[ResponsesOutputText]


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
    output: list[ResponsesOutputMessage]
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


def translate_responses_request(payload: dict) -> dict:
    try:
        req = ResponsesCreateRequest.model_validate(payload)
    except ValidationError as exc:
        raise ResponsesCompatError(
            "Request body does not match the supported OpenAI Responses subset"
        ) from exc

    if req.tools:
        raise ResponsesCompatError("Responses tools are not supported yet")
    if req.tool_choice is not None:
        raise ResponsesCompatError("Responses tool_choice is not supported yet")
    if req.previous_response_id is not None:
        raise ResponsesCompatError("previous_response_id is not supported yet")

    messages: list[dict[str, str]] = []

    if req.instructions is not None:
        instructions_text = _content_to_text(req.instructions, field_name="instructions")
        messages.append({"role": "system", "content": instructions_text})

    if isinstance(req.input, str):
        messages.append({"role": "user", "content": req.input})
    else:
        for index, item in enumerate(req.input):
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

    body: dict[str, Any] = {
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


def _assistant_text_from_chat_response(body: dict) -> str:
    for choice in body.get("choices", []):
        message = choice.get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            fragments = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            return "".join(fragments)
    return ""


def translate_chat_response(body: dict) -> dict:
    chat_id = str(body.get("id") or "chatcmpl-generated")
    response_id = chat_id if chat_id.startswith("resp_") else f"resp_{chat_id}"
    message_id = response_id.replace("resp_", "msg_", 1)
    assistant_text = _assistant_text_from_chat_response(body)
    usage = body.get("usage", {})
    choices = body.get("choices") or []
    finish_reason = choices[0].get("finish_reason") if choices else None
    status, incomplete_details = _status_from_finish_reason(finish_reason)

    response = ResponsesCreateResponse(
        id=response_id,
        created_at=int(body.get("created") or datetime.now(UTC).timestamp()),
        status=status,
        model=str(body.get("model") or ""),
        output=[
            ResponsesOutputMessage(
                id=message_id,
                status=status,
                content=[ResponsesOutputText(text=assistant_text)],
            )
        ],
        output_text=assistant_text,
        incomplete_details=incomplete_details,
        usage=ResponsesUsage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        ),
    )
    return response.model_dump()


def _sse_frame(event_type: str, **fields: Any) -> str:
    payload = {"type": event_type, **fields}
    return f"data: {json.dumps(payload)}\n\n"


async def openai_sse_to_responses_sse(body_iterator, model: str):
    """Translate upstream OpenAI chat-completions SSE chunks into Responses API SSE events.

    Structurally mirrors ``openai_sse_to_anthropic_sse`` in ``anthropic_compat.py``, but
    targets the Responses protocol's flat ``data: {"type": ...}`` framing (no separate
    ``event:`` line) and its event vocabulary (``response.created``,
    ``response.output_text.delta``, ``response.completed``, ...). Reuses
    ``_iter_sse_json`` for parsing so a ``data:`` line split across two network
    reads is reassembled instead of silently dropped.
    """
    response_id = f"resp_{uuid4().hex}"
    message_id = f"msg_{uuid4().hex}"
    created_at = int(datetime.now(UTC).timestamp())
    output_index = 0
    content_index = 0
    final_model = model
    finish_reason: str | None = None
    text_fragments: list[str] = []

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
    yield _sse_frame(
        "response.output_item.added",
        output_index=output_index,
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
        output_index=output_index,
        content_index=content_index,
        part={"type": "output_text", "text": "", "annotations": []},
    )

    async for event in _iter_sse_json(body_iterator):
        if event.get("model"):
            final_model = event["model"]

        choices = event.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        chunk_finish_reason = choices[0].get("finish_reason")
        if chunk_finish_reason:
            finish_reason = chunk_finish_reason

        text_delta = delta.get("content")
        if text_delta:
            text_fragments.append(text_delta)
            yield _sse_frame(
                "response.output_text.delta",
                item_id=message_id,
                output_index=output_index,
                content_index=content_index,
                delta=text_delta,
            )

    final_text = "".join(text_fragments)
    status, incomplete_details = _status_from_finish_reason(finish_reason)

    yield _sse_frame(
        "response.output_text.done",
        item_id=message_id,
        output_index=output_index,
        content_index=content_index,
        text=final_text,
    )

    final_output_text = ResponsesOutputText(text=final_text)
    yield _sse_frame(
        "response.content_part.done",
        item_id=message_id,
        output_index=output_index,
        content_index=content_index,
        part=final_output_text.model_dump(),
    )

    final_message = ResponsesOutputMessage(id=message_id, status=status, content=[final_output_text])
    yield _sse_frame(
        "response.output_item.done",
        output_index=output_index,
        item=final_message.model_dump(),
    )

    final_response = ResponsesCreateResponse(
        id=response_id,
        created_at=created_at,
        status=status,
        model=final_model,
        output=[final_message],
        output_text=final_text,
        incomplete_details=incomplete_details,
    )
    yield _sse_frame("response.completed", response=final_response.model_dump())
