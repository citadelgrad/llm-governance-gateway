"""Provider-neutral streaming events and wire-protocol stream translations."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import cast

from proxy.app.openai_chat_stream import iter_openai_chat_events
from proxy.app.protocol_types import (
    CanonicalStreamEvent,
    CanonicalStreamFailed,
    CanonicalStreamMessageCompleted,
    CanonicalStreamMessageStarted,
    CanonicalStreamTerminalReason,
    CanonicalStreamTextDelta,
    CanonicalStreamToolCallArgumentsDelta,
    CanonicalStreamToolCallStarted,
    CanonicalStreamUsageUpdate,
    CanonicalStreamUsageUpdated,
    JsonObject,
    OpenAIChatCompletionChunk,
)

_ANTHROPIC_STOP_REASON_TO_CANONICAL: dict[str, CanonicalStreamTerminalReason] = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "tool_use": "tool_use",
    "stop_sequence": "end_turn",
}

_CHAT_FINISH_REASON_TO_CANONICAL: dict[str, CanonicalStreamTerminalReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "content_filtered",
}

_CANONICAL_REASON_TO_CHAT_FINISH: dict[CanonicalStreamTerminalReason, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "content_filtered": "content_filter",
    "cancelled": "stop",
    "error": "stop",
    "unknown": "stop",
}


def _int_field(obj: object, key: str) -> int:
    if not isinstance(obj, dict):
        return 0
    value = cast(JsonObject, obj).get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def iter_anthropic_messages_canonical_events(
    lines: AsyncIterator[str],
) -> AsyncIterator[CanonicalStreamEvent]:
    """Map Anthropic Messages SSE data lines into canonical stream events."""
    tool_block_index: dict[int, int] = {}
    tool_call_counter = 0

    async for line in lines:
        if not line.startswith("data:"):
            continue
        data_str = line[len("data:") :].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            yield CanonicalStreamFailed(
                error_type="invalid_upstream_stream",
                error_message="Provider stream contained malformed JSON",
            )
            return
        if not isinstance(event, dict):
            yield CanonicalStreamFailed(
                error_type="invalid_upstream_stream",
                error_message="Provider stream event was not a JSON object",
            )
            return

        event_object = cast(JsonObject, event)
        etype = event_object.get("type", "")

        if etype == "error":
            raw_error = event_object.get("error")
            error = cast(JsonObject, raw_error) if isinstance(raw_error, dict) else {}
            error_type = error.get("type")
            error_message = error.get("message")
            yield CanonicalStreamFailed(
                error_type=error_type if isinstance(error_type, str) else "provider_stream_error",
                error_message=(
                    error_message if isinstance(error_message, str) else "Provider stream failed"
                ),
            )
            return

        if etype == "message_start":
            raw_message = event_object.get("message")
            message = cast(JsonObject, raw_message) if isinstance(raw_message, dict) else {}
            message_id = message.get("id")
            model = message.get("model")
            yield CanonicalStreamMessageStarted(
                message_id=message_id if isinstance(message_id, str) else "",
                model=model if isinstance(model, str) else None,
            )
            input_tokens = _int_field(message.get("usage"), "input_tokens")
            if input_tokens:
                yield CanonicalStreamUsageUpdated(
                    usage=CanonicalStreamUsageUpdate(input_tokens=input_tokens)
                )
            continue

        if etype == "content_block_start":
            raw_content_block = event_object.get("content_block")
            content_block = (
                cast(JsonObject, raw_content_block) if isinstance(raw_content_block, dict) else {}
            )
            if content_block.get("type") != "tool_use":
                continue
            raw_block_index = event_object.get("index", 0)
            block_index = raw_block_index if isinstance(raw_block_index, int) else 0
            tool_index = tool_call_counter
            tool_block_index[block_index] = tool_index
            tool_call_counter += 1
            call_id = content_block.get("id")
            name = content_block.get("name")
            yield CanonicalStreamToolCallStarted(
                tool_index=tool_index,
                call_id=call_id if isinstance(call_id, str) else "",
                name=name if isinstance(name, str) else "",
            )
            continue

        if etype == "content_block_delta":
            raw_delta = event_object.get("delta")
            delta = cast(JsonObject, raw_delta) if isinstance(raw_delta, dict) else {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text")
                yield CanonicalStreamTextDelta(text=text if isinstance(text, str) else "")
            elif delta_type == "input_json_delta":
                raw_block_index = event_object.get("index", 0)
                block_index = raw_block_index if isinstance(raw_block_index, int) else 0
                tool_index = tool_block_index.get(block_index)
                partial_json = delta.get("partial_json")
                if tool_index is not None:
                    yield CanonicalStreamToolCallArgumentsDelta(
                        tool_index=tool_index,
                        arguments_delta=partial_json if isinstance(partial_json, str) else "",
                    )
            continue

        if etype == "message_delta":
            output_tokens = _int_field(event_object.get("usage"), "output_tokens")
            if output_tokens:
                yield CanonicalStreamUsageUpdated(
                    usage=CanonicalStreamUsageUpdate(output_tokens=output_tokens)
                )
            raw_delta = event_object.get("delta")
            delta = cast(JsonObject, raw_delta) if isinstance(raw_delta, dict) else {}
            raw_stop_reason = delta.get("stop_reason", "end_turn")
            stop_reason = raw_stop_reason if isinstance(raw_stop_reason, str) else "end_turn"
            reason = _ANTHROPIC_STOP_REASON_TO_CANONICAL.get(stop_reason, "unknown")
            status = "incomplete" if reason == "max_tokens" else "completed"
            yield CanonicalStreamMessageCompleted(status=status, reason=reason)
            continue


async def iter_openai_chat_canonical_events(
    body_iterator: AsyncIterator[str | bytes],
) -> AsyncIterator[CanonicalStreamEvent]:
    """Map strict OpenAI Chat SSE chunks into canonical stream events."""
    message_started = False

    async for decoded_event in iter_openai_chat_events(body_iterator):
        if isinstance(decoded_event, dict):
            raw_error = decoded_event.get("error")
            error = cast(JsonObject, raw_error) if isinstance(raw_error, dict) else {}
            error_type = error.get("type")
            error_message = error.get("message")
            yield CanonicalStreamFailed(
                error_type=error_type if isinstance(error_type, str) else "provider_stream_error",
                error_message=(
                    error_message if isinstance(error_message, str) else "Provider stream failed"
                ),
            )
            return

        chunk: OpenAIChatCompletionChunk = decoded_event
        if not message_started:
            yield CanonicalStreamMessageStarted(message_id=chunk.id, model=chunk.model)
            message_started = True

        if chunk.usage is not None:
            yield CanonicalStreamUsageUpdated(
                usage=CanonicalStreamUsageUpdate(
                    input_tokens=chunk.usage.prompt_tokens,
                    output_tokens=chunk.usage.completion_tokens,
                    total_tokens=chunk.usage.total_tokens,
                )
            )

        if not chunk.choices:
            continue

        choice = chunk.choices[0]
        delta = choice.delta
        if delta.content:
            yield CanonicalStreamTextDelta(text=delta.content)

        if delta.tool_calls:
            for tool_call in delta.tool_calls:
                function = tool_call.function
                call_id = tool_call.id
                name = function.name if function is not None else None
                if call_id is not None or name is not None:
                    yield CanonicalStreamToolCallStarted(
                        tool_index=tool_call.index,
                        call_id=call_id or "",
                        name=name or "",
                    )
                arguments = function.arguments if function is not None else None
                if arguments:
                    yield CanonicalStreamToolCallArgumentsDelta(
                        tool_index=tool_call.index,
                        arguments_delta=arguments,
                    )

        if choice.finish_reason is not None:
            reason = _CHAT_FINISH_REASON_TO_CANONICAL[choice.finish_reason]
            status = "incomplete" if reason == "max_tokens" else "completed"
            yield CanonicalStreamMessageCompleted(status=status, reason=reason)


async def iter_openai_chat_sse_from_canonical(
    events: AsyncIterator[CanonicalStreamEvent],
    *,
    model: str,
) -> AsyncIterator[str]:
    """Encode canonical events as OpenAI Chat Completions SSE chunks."""
    completion_id = ""
    input_tokens = 0
    output_tokens = 0

    async for event in events:
        if isinstance(event, CanonicalStreamMessageStarted):
            completion_id = event.message_id
            continue

        if isinstance(event, CanonicalStreamUsageUpdated):
            if event.usage.input_tokens is not None:
                input_tokens = event.usage.input_tokens
            if event.usage.output_tokens is not None:
                output_tokens = event.usage.output_tokens
            continue

        chunk: JsonObject | None = None
        if isinstance(event, CanonicalStreamTextDelta):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": event.text}, "finish_reason": None}],
            }
        elif isinstance(event, CanonicalStreamToolCallStarted):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": event.tool_index,
                                    "id": event.call_id,
                                    "type": "function",
                                    "function": {"name": event.name, "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        elif isinstance(event, CanonicalStreamToolCallArgumentsDelta):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": event.tool_index,
                                    "function": {"arguments": event.arguments_delta},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        elif isinstance(event, CanonicalStreamMessageCompleted):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": _CANONICAL_REASON_TO_CHAT_FINISH[event.reason],
                    }
                ],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            }
        elif isinstance(event, CanonicalStreamFailed):
            yield (
                "data: "
                + json.dumps(
                    {"error": {"type": event.error_type, "message": event.error_message}},
                    separators=(",", ":"),
                )
                + "\n\n"
            )
            return

        if chunk is not None:
            yield f"data: {json.dumps(chunk)}\n\n"
            if isinstance(event, CanonicalStreamMessageCompleted):
                yield "data: [DONE]\n\n"

