from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


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
    status: Literal["completed"] = "completed"
    role: Literal["assistant"] = "assistant"
    content: list[ResponsesOutputText]


class ResponsesUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_tokens_details: dict[str, int] = Field(default_factory=lambda: {"cached_tokens": 0})
    output_tokens_details: dict[str, int] = Field(default_factory=lambda: {"reasoning_tokens": 0})


class ResponsesCreateResponse(BaseModel):
    id: str
    object: Literal["response"] = "response"
    created_at: int
    status: Literal["completed"] = "completed"
    model: str
    output: list[ResponsesOutputMessage]
    output_text: str = ""
    error: None = None
    incomplete_details: None = None
    usage: ResponsesUsage = Field(default_factory=ResponsesUsage)


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

    if req.stream:
        raise ResponsesCompatError("Streaming is not supported on /v1/responses")
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
        "stream": False,
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

    response = ResponsesCreateResponse(
        id=response_id,
        created_at=int(body.get("created") or datetime.now(UTC).timestamp()),
        model=str(body.get("model") or ""),
        output=[
            ResponsesOutputMessage(
                id=message_id,
                content=[ResponsesOutputText(text=assistant_text)],
            )
        ],
        output_text=assistant_text,
        usage=ResponsesUsage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        ),
    )
    return response.model_dump()
