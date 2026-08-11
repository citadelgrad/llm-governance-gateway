from __future__ import annotations

import json

from proxy.app.protocol_types import (
    CanonicalStreamFailed,
    CanonicalStreamMessageCompleted,
    CanonicalStreamMessageStarted,
    CanonicalStreamTextDelta,
    CanonicalStreamToolCallArgumentsDelta,
    CanonicalStreamToolCallStarted,
    CanonicalStreamUsageUpdated,
)
from proxy.app.stream_events import (
    iter_anthropic_messages_canonical_events,
    iter_openai_chat_canonical_events,
    iter_openai_chat_sse_from_canonical,
)


async def _lines(*lines: str):
    for line in lines:
        yield line


async def _chunks(*chunks: bytes):
    for chunk in chunks:
        yield chunk


async def _collect(events):
    return [event async for event in events]


def _chat_sse(payload: dict) -> bytes:
    event = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "translated-model",
        **payload,
    }
    return f"data: {json.dumps(event)}\n\n".encode()


async def test_anthropic_messages_stream_maps_to_canonical_tool_usage_and_terminal():
    events = await _collect(
        iter_anthropic_messages_canonical_events(
            _lines(
                'data: {"type": "message_start", "message": {"id": "msg_1", '
                '"model": "claude-sonnet", "usage": {"input_tokens": 12}}}',
                'data: {"type": "content_block_start", "index": 4, '
                '"content_block": {"type": "tool_use", "id": "toolu_1", "name": "weather"}}',
                'data: {"type": "content_block_delta", "index": 4, '
                '"delta": {"type": "input_json_delta", "partial_json": "{\\"city\\":"}}',
                'data: {"type": "content_block_delta", "index": 0, '
                '"delta": {"type": "text_delta", "text": "hi"}}',
                'data: {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, '
                '"usage": {"output_tokens": 4}}',
                "data: [DONE]",
            )
        )
    )

    assert [event.kind for event in events] == [
        "message_started",
        "usage_updated",
        "tool_call_started",
        "tool_call_arguments_delta",
        "text_delta",
        "usage_updated",
        "message_completed",
    ]
    assert isinstance(events[0], CanonicalStreamMessageStarted)
    assert events[0].message_id == "msg_1"
    assert isinstance(events[2], CanonicalStreamToolCallStarted)
    assert events[2].tool_index == 0
    assert events[2].call_id == "toolu_1"
    assert isinstance(events[3], CanonicalStreamToolCallArgumentsDelta)
    assert events[3].arguments_delta == '{"city":'
    assert isinstance(events[-1], CanonicalStreamMessageCompleted)
    assert events[-1].reason == "tool_use"


async def test_openai_chat_stream_maps_to_canonical_text_usage_and_terminal():
    events = await _collect(
        iter_openai_chat_canonical_events(
            _chunks(
                _chat_sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "hello"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
                _chat_sse(
                    {
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "length"}
                        ],
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 3,
                            "total_tokens": 5,
                        },
                    }
                ),
                b"data: [DONE]\n\n",
            )
        )
    )

    assert [event.kind for event in events] == [
        "message_started",
        "text_delta",
        "usage_updated",
        "message_completed",
    ]
    assert isinstance(events[1], CanonicalStreamTextDelta)
    assert events[1].text == "hello"
    assert isinstance(events[2], CanonicalStreamUsageUpdated)
    assert events[2].usage.total_tokens == 5
    assert isinstance(events[-1], CanonicalStreamMessageCompleted)
    assert events[-1].status == "incomplete"
    assert events[-1].reason == "max_tokens"


async def test_openai_chat_stream_malformed_json_maps_to_canonical_failure():
    events = await _collect(
        iter_openai_chat_canonical_events(_chunks(b'data: {"choices": [}\n\n'))
    )

    assert len(events) == 1
    assert isinstance(events[0], CanonicalStreamFailed)
    assert events[0].error_type == "invalid_upstream_stream"


async def test_canonical_events_encode_to_openai_chat_sse_with_usage_and_done():
    frames = await _collect(
        iter_openai_chat_sse_from_canonical(
            _lines_from_events(
                CanonicalStreamMessageStarted(message_id="msg_1"),
                CanonicalStreamUsageUpdated(
                    usage={"input_tokens": 2, "output_tokens": None, "total_tokens": None}
                ),
                CanonicalStreamTextDelta(text="hi"),
                CanonicalStreamUsageUpdated(
                    usage={"input_tokens": None, "output_tokens": 3, "total_tokens": None}
                ),
                CanonicalStreamMessageCompleted(reason="end_turn"),
            ),
            model="claude-sonnet",
        )
    )

    payloads = [
        json.loads(frame.removeprefix("data: ").strip())
        for frame in frames
        if not frame.endswith("[DONE]\n\n")
    ]
    assert payloads[0]["choices"][0]["delta"]["content"] == "hi"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1]["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 3,
        "total_tokens": 5,
    }
    assert frames[-1] == "data: [DONE]\n\n"


async def _lines_from_events(*events):
    for event in events:
        yield event

