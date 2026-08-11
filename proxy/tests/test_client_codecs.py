from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from proxy.app.client_codecs import (
    AnthropicMessagesCodec,
    OpenAIChatCodec,
    PipelineResult,
    ResponsesCodec,
)
from proxy.app.protocol_types import JsonObject
from starlette.responses import Response, StreamingResponse


def _chat_response(**overrides) -> JsonObject:
    body: JsonObject = {
        "id": "chatcmpl-test",
        "created": 1,
        "model": "gpt-test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }
    body.update(overrides)
    return body


async def _chat_sse():
    yield (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "gpt-test",
                "choices": [
                    {"index": 0, "delta": {"content": "hi"}, "finish_reason": None}
                ],
            }
        )
        + "\n\n"
    )
    yield (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "gpt-test",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            }
        )
        + "\n\n"
    )
    yield "data: [DONE]\n\n"


async def _collect_stream(response: StreamingResponse) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode())
        else:
            chunks.append(chunk)
    return "".join(chunks)


def test_chat_codec_owns_validation_error_shape():
    with pytest.raises(HTTPException) as raised:
        OpenAIChatCodec().decode_payload({"model": "gpt-test", "messages": [], "unknown": True})

    detail = raised.value.detail
    assert detail["error"]["type"] == "invalid_request"
    assert detail["error"]["details"]["violations"][0]["field"] == "unknown"


def test_responses_codec_owns_payload_and_native_response_passthrough():
    codec = ResponsesCodec()
    payload = codec.decode_payload({"model": "gpt-test", "input": "hello"})
    response = Response(content=b"native", media_type="application/json")

    encoded = codec.encode_response(
        PipelineResult(response, {}, "openai_responses", payload.model)
    )

    assert payload.protocol == "openai_responses"
    assert payload.native_providers == frozenset({"openai"})
    assert encoded is response


def test_responses_codec_translates_buffered_chat_response_and_rebuilds_headers():
    codec = ResponsesCodec()
    provider_response = Response(
        content=json.dumps(_chat_response()),
        media_type="application/json",
        headers={"content-length": "999", "x-provider": "kept"},
    )

    encoded = codec.encode_response(
        PipelineResult(provider_response, {}, "openai_chat", "gpt-test")
    )

    assert isinstance(encoded, Response)
    assert encoded.headers["x-provider"] == "kept"
    assert encoded.headers["content-length"] != "999"
    assert encoded.media_type == "application/json"
    assert json.loads(bytes(encoded.body))["object"] == "response"


async def test_responses_codec_translates_chat_stream_to_responses_sse():
    encoded = ResponsesCodec().encode_response(
        PipelineResult(
            StreamingResponse(_chat_sse(), media_type="text/event-stream"),
            {"x-audit-id": "audit-1"},
            "openai_chat",
            "gpt-test",
        )
    )

    assert isinstance(encoded, StreamingResponse)
    body = await _collect_stream(encoded)
    assert "response.created" in body
    assert "response.output_text.delta" in body
    assert encoded.headers["x-audit-id"] == "audit-1"


def test_anthropic_codec_owns_validation_error_shape():
    with pytest.raises(HTTPException) as raised:
        AnthropicMessagesCodec().decode_payload(
            {"model": "claude-test", "messages": [], "max_tokens": 10, "unknown": True}
        )

    detail = raised.value.detail
    assert detail["error"]["type"] == "invalid_request"
    assert detail["violations"][0]["field"] == "unknown"


def test_anthropic_codec_translates_buffered_chat_response():
    codec = AnthropicMessagesCodec()
    provider_response = Response(
        content=json.dumps(_chat_response()),
        media_type="application/json",
        headers={"x-provider": "kept"},
    )

    encoded = codec.encode_response(
        PipelineResult(provider_response, {"x-audit-id": "audit-1"}, "openai_chat", "claude-test")
    )

    assert isinstance(encoded, Response)
    body = json.loads(bytes(encoded.body))
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "hello"}]
    assert encoded.headers["x-provider"] == "kept"
    assert encoded.headers["x-audit-id"] == "audit-1"
