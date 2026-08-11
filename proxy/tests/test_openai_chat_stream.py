from __future__ import annotations

import json

from proxy.app.openai_chat_stream import iter_openai_chat_events
from proxy.app.protocol_types import OpenAIChatCompletionChunk


async def _sse_body(*raw_chunks: str | bytes):
    for chunk in raw_chunks:
        yield chunk


def _chat_chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    chunk_id: str = "chatcmpl-test",
) -> dict:
    delta = {} if content is None else {"content": content}
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "translated-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _chat_sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _decoded_events(*raw_chunks: str | bytes):
    return [event async for event in iter_openai_chat_events(_sse_body(*raw_chunks))]


def _decoded_error(event: object) -> dict:
    assert isinstance(event, dict)
    error = event["error"]
    assert isinstance(error, dict)
    return error


async def test_iter_openai_chat_events_buffers_fragmented_data_line():
    full_line = _chat_sse(_chat_chunk(content="Hello world"))
    split_at = len(full_line) // 2

    events = await _decoded_events(full_line[:split_at], full_line[split_at:])

    assert len(events) == 1
    chunk = events[0]
    assert isinstance(chunk, OpenAIChatCompletionChunk)
    assert chunk.choices[0].delta.content == "Hello world"


async def test_iter_openai_chat_events_preserves_split_multibyte_utf8():
    line = _chat_sse(_chat_chunk(content="hello 🌿")).encode()
    split_at = line.index("🌿".encode()) + 1

    events = await _decoded_events(line[:split_at], line[split_at:])

    chunk = events[0]
    assert isinstance(chunk, OpenAIChatCompletionChunk)
    assert chunk.choices[0].delta.content == "hello 🌿"


async def test_iter_openai_chat_events_rejects_malformed_json_once():
    events = await _decoded_events('data: {"choices": [}\n\n', _chat_sse(_chat_chunk(content="ignored")))

    assert len(events) == 1
    assert _decoded_error(events[0])["type"] == "invalid_upstream_stream"
    assert _decoded_error(events[0])["message"] == "Provider stream contained malformed JSON"


async def test_iter_openai_chat_events_rejects_non_object_json_event():
    events = await _decoded_events('data: ["not", "object"]\n\n')

    assert len(events) == 1
    assert _decoded_error(events[0]) == {
        "type": "invalid_upstream_stream",
        "message": "Provider stream event was not a JSON object",
    }


async def test_iter_openai_chat_events_rejects_invalid_typed_chat_chunk():
    events = await _decoded_events('data: {"choices": []}\n\n')

    assert len(events) == 1
    assert _decoded_error(events[0]) == {
        "type": "invalid_upstream_stream",
        "message": "Provider stream chunk did not match the Chat protocol",
    }


async def test_iter_openai_chat_events_passes_through_upstream_error_as_terminal_event():
    upstream_error = {"error": {"type": "upstream_timeout", "message": "timed out"}}

    events = await _decoded_events(
        f"data: {json.dumps(upstream_error)}\n\n",
        _chat_sse(_chat_chunk(content="ignored")),
    )

    assert events == [upstream_error]


async def test_iter_openai_chat_events_rejects_invalid_utf8():
    events = await _decoded_events(b"data: \xff\n\n")

    assert len(events) == 1
    assert _decoded_error(events[0]) == {
        "type": "invalid_upstream_stream",
        "message": "Provider stream contained invalid UTF-8",
    }


async def test_iter_openai_chat_events_rejects_incomplete_utf8():
    events = await _decoded_events(b"data: " + "🌿".encode()[:1])

    assert len(events) == 1
    assert _decoded_error(events[0]) == {
        "type": "invalid_upstream_stream",
        "message": "Provider stream ended with incomplete UTF-8",
    }


async def test_iter_openai_chat_events_rejects_chunk_type_change_during_utf8_sequence():
    events = await _decoded_events(b"data: " + "🌿".encode()[:1], "{}\n\n")

    assert len(events) == 1
    assert _decoded_error(events[0]) == {
        "type": "invalid_upstream_stream",
        "message": "Provider stream changed chunk type during UTF-8 sequence",
    }


async def test_iter_openai_chat_events_decodes_final_data_line_without_trailing_newline():
    events = await _decoded_events(_chat_sse(_chat_chunk(content="final")).rstrip())

    chunk = events[0]
    assert isinstance(chunk, OpenAIChatCompletionChunk)
    assert chunk.choices[0].delta.content == "final"
