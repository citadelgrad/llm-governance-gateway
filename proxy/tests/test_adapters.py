from __future__ import annotations

import json

import httpx
import pytest
from proxy.app.anthropic_compat import (
    AnthropicCompatError,
    AnthropicGatewayPayload,
    AnthropicImageBlock,
    AnthropicMessagesRequest,
    AnthropicRedactedThinkingBlock,
    AnthropicTextBlock,
    AnthropicThinkingBlock,
    AnthropicToolDefinition,
    AnthropicToolResultBlock,
    AnthropicToolUseBlock,
    messages_to_chat_body,
    openai_sse_to_anthropic_sse,
)
from proxy.app.providers import (
    anthropic as anthropic_provider,
)
from proxy.app.providers import (
    gemini as gemini_provider,
)
from proxy.app.providers import (
    generic as generic_provider,
)
from proxy.app.providers import (
    ollama as ollama_provider,
)
from proxy.app.providers import (
    openai as openai_provider,
)
from proxy.app.providers.native import forward_native
from proxy.app.providers.usage import UsageMetrics, extract_usage
from pydantic import ValidationError
from starlette.responses import StreamingResponse

# ---------------------------------------------------------------------------
# extract_usage
# ---------------------------------------------------------------------------


def test_extract_usage_openai():
    body = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    assert extract_usage("openai", body) == UsageMetrics(10, 5, 15)


def test_extract_usage_ollama_alias_uses_openai_shape():
    body = {"usage": {"prompt_tokens": 4, "completion_tokens": 7, "total_tokens": 11}}
    assert extract_usage("ollama", body) == UsageMetrics(4, 7, 11)


def test_extract_usage_generic_alias_uses_openai_shape():
    body = {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
    assert extract_usage("generic", body) == UsageMetrics(1, 2, 3)


def test_extract_usage_openai_total_fallback():
    body = {"usage": {"prompt_tokens": 4, "completion_tokens": 6}}
    assert extract_usage("openai", body) == UsageMetrics(4, 6, 10)


def test_extract_usage_anthropic():
    body = {"usage": {"input_tokens": 20, "output_tokens": 3}}
    assert extract_usage("anthropic", body) == UsageMetrics(20, 3, 23)


def test_extract_usage_gemini():
    body = {
        "usageMetadata": {
            "promptTokenCount": 8,
            "candidatesTokenCount": 12,
            "totalTokenCount": 20,
        }
    }
    assert extract_usage("gemini", body) == UsageMetrics(8, 12, 20)


def test_extract_usage_gemini_total_fallback():
    body = {"usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 12}}
    assert extract_usage("gemini", body) == UsageMetrics(8, 12, 20)


def test_extract_usage_unknown_provider_returns_zero():
    assert extract_usage("unknown", {"usage": {"prompt_tokens": 99}}) == UsageMetrics.zero()


def test_extract_usage_non_dict_returns_zero():
    assert extract_usage("openai", "not-a-dict") == UsageMetrics.zero()  # type: ignore[arg-type]


def test_extract_usage_missing_usage_returns_zero():
    assert extract_usage("openai", {}) == UsageMetrics.zero()
    assert extract_usage("anthropic", {}) == UsageMetrics.zero()
    assert extract_usage("gemini", {}) == UsageMetrics.zero()


# ---------------------------------------------------------------------------
# OpenAI — modern chat request compatibility and streaming errors
# ---------------------------------------------------------------------------


async def test_openai_native_chat_preserves_request_body(httpx_mock):
    body = {
        "model": "gpt-5.6-luna",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "max_tokens": 128,
    }
    httpx_mock.add_response(
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        json={"id": "chatcmpl_1", "choices": [], "usage": {}},
    )
    client = openai_provider.make_client("test-key")
    try:
        response = await openai_provider.chat_completions(client, body, False, {})
    finally:
        await client.aclose()

    assert response.status_code == 200
    request = httpx_mock.get_request()
    assert request is not None
    assert json.loads(request.content) == body


async def test_openai_streaming_propagates_upstream_error_status(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        status_code=400,
        json={
            "error": {
                "message": "Unsupported parameter",
                "type": "invalid_request_error",
            }
        },
    )
    client = openai_provider.make_client("test-key")
    try:
        response = await openai_provider.chat_completions(
            client,
            {
                "model": "gpt-5.6-luna",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            stream=True,
            extra_headers={"X-Audit-ID": "audit-1"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 400
    assert response.media_type == "application/json"
    assert json.loads(response.body)["error"]["type"] == "invalid_request_error"
    assert response.headers["x-audit-id"] == "audit-1"


@pytest.mark.parametrize(
    ("provider", "base_url", "body"),
    [
        (
            anthropic_provider,
            "https://api.anthropic.test",
            {"model": "claude-test", "messages": [{"role": "user", "content": "hi"}]},
        ),
        (
            gemini_provider,
            "https://generativelanguage.test",
            {
                "model": "gemini-test",
                "messages": [{"role": "user", "content": "hi"}],
            },
        ),
        (
            ollama_provider,
            "http://ollama.test/v1",
            {"model": "local-test", "messages": [{"role": "user", "content": "hi"}]},
        ),
    ],
)
async def test_translated_streams_propagate_upstream_error_status(provider, base_url, body):
    async def handler(_request):
        return httpx.Response(
            429,
            json={"error": {"message": "rate limited"}},
            headers={"content-type": "application/json"},
        )

    async with httpx.AsyncClient(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
    ) as client:
        response = await provider.chat_completions(client, body, True, {})

    assert response.status_code == 429
    assert not isinstance(response, StreamingResponse)


async def test_openai_streaming_forwards_valid_sse_and_translated_body(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        headers={"content-type": "text/event-stream"},
        content=(
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b"data: [DONE]\n\n"
        ),
    )
    client = openai_provider.make_client("test-key")
    try:
        response = await openai_provider.chat_completions(
            client,
            {
                "model": "gpt-5.6-luna",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 32,
                "stream": True,
            },
            stream=True,
            extra_headers={},
        )
        content = b"".join([chunk async for chunk in response.body_iterator])
    finally:
        await client.aclose()

    sent_body = json.loads(httpx_mock.get_requests()[0].content)
    assert sent_body["max_tokens"] == 32
    assert "max_completion_tokens" not in sent_body
    assert b'"content":"ok"' in content
    assert b"data: [DONE]" in content


async def test_native_forwarding_preserves_safe_upstream_headers(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://provider.test/v1/responses",
        headers={
            "content-type": "application/json",
            "x-request-id": "req_provider_1",
            "x-ratelimit-remaining-requests": "9",
            "set-cookie": "must-not-leak=true",
        },
        json={"id": "resp_1"},
    )
    async with httpx.AsyncClient(base_url="https://provider.test/v1") as client:
        response = await forward_native(
            client,
            path="/responses",
            body={"model": "model", "input": "hello"},
            stream=False,
            extra_headers={"x-audit-id": "audit_1"},
            provider="openai",
        )

    assert response.headers["x-request-id"] == "req_provider_1"
    assert response.headers["x-ratelimit-remaining-requests"] == "9"
    assert response.headers["x-audit-id"] == "audit_1"
    assert "set-cookie" not in response.headers


async def test_native_stream_rejects_successful_non_sse_response(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://provider.test/v1/responses",
        headers={"content-type": "application/json"},
        json={"error": {"message": "not actually a stream"}},
    )
    async with httpx.AsyncClient(base_url="https://provider.test/v1") as client:
        response = await forward_native(
            client,
            path="/responses",
            body={"model": "model", "input": "hello", "stream": True},
            stream=True,
            extra_headers={},
            provider="openai",
        )

    assert response.status_code == 502
    assert response.body == (
        b'{"error":{"type":"invalid_upstream_stream","message":"Upstream returned a non-SSE response"}}'
    )


# ---------------------------------------------------------------------------
# Anthropic — request translation
# ---------------------------------------------------------------------------


def test_anthropic_translate_extracts_system_message():
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hi"},
        ],
    }
    out = anthropic_provider._translate_request(body)
    assert out["system"] == "Be brief."
    # System message is stripped from messages array
    assert out["messages"] == [{"role": "user", "content": "Hi"}]
    # Default max_tokens applied
    assert out["max_tokens"] == 1024


def test_anthropic_translate_joins_multiple_system_messages():
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [
            {"role": "system", "content": "First."},
            {"role": "system", "content": "Second."},
            {"role": "user", "content": "Hi"},
        ],
    }
    out = anthropic_provider._translate_request(body)
    assert out["system"] == "First.\n\nSecond."


def test_anthropic_translate_forwards_extended_thinking():
    body = {
        "model": "claude-haiku-4-5-20251001",
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 8192,
        "thinking": {"type": "enabled", "budget_tokens": 1024},
    }

    out = anthropic_provider._translate_request(body)

    assert out["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_anthropic_translate_tool_call_assistant_message():
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "72F"},
        ],
    }
    out = anthropic_provider._translate_request(body)
    # Assistant tool-call message becomes a content-blocks message with tool_use
    assistant_msg = out["messages"][1]
    assert assistant_msg["role"] == "assistant"
    assert any(
        block.get("type") == "tool_use" and block.get("id") == "call_1"
        for block in assistant_msg["content"]
    )
    # Tool result becomes a user message with a tool_result block
    tool_result_msg = out["messages"][2]
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"][0]["type"] == "tool_result"
    assert tool_result_msg["content"][0]["tool_use_id"] == "call_1"


def test_anthropic_translate_function_tools_and_choice():
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}},
                    "strict": True,
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
    }

    out = anthropic_provider._translate_request(body)

    assert out["tools"] == [
        {
            "name": "get_weather",
            "description": "Get weather",
            "input_schema": {"type": "object", "properties": {}},
            "strict": True,
        }
    ]
    assert out["tool_choice"] == {"type": "tool", "name": "get_weather"}


# ---------------------------------------------------------------------------
# Anthropic — content-block typed union (ai-gateway-b7k.2)
# ---------------------------------------------------------------------------


def test_anthropic_content_block_union_dispatches_each_block_type():
    request = AnthropicMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 100,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "inspecting"},
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
                        },
                        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}},
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "contents",
                        },
                        {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                        {"type": "redacted_thinking", "data": "opaque"},
                    ],
                }
            ],
        }
    )

    blocks = request.messages[0].content
    assert isinstance(blocks, list)
    text, image, tool_use, tool_result, thinking, redacted = blocks
    assert isinstance(text, AnthropicTextBlock)
    assert isinstance(image, AnthropicImageBlock)
    assert isinstance(tool_use, AnthropicToolUseBlock)
    assert isinstance(tool_result, AnthropicToolResultBlock)
    assert isinstance(thinking, AnthropicThinkingBlock)
    assert isinstance(redacted, AnthropicRedactedThinkingBlock)


def test_anthropic_content_block_rejects_unknown_field():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AnthropicMessagesRequest.model_validate(
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 100,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "hi", "silently_lost": True}],
                    }
                ],
            }
        )


def test_anthropic_content_block_rejects_unknown_block_type():
    with pytest.raises(ValidationError):
        AnthropicMessagesRequest.model_validate(
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 100,
                "messages": [
                    {"role": "user", "content": [{"type": "video", "source": {}}]},
                ],
            }
        )


def test_anthropic_tool_definition_preserves_typed_input_schema():
    tool = AnthropicToolDefinition.model_validate(
        {
            "name": "get_weather",
            "description": "Get weather",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    )
    assert tool.input_schema["required"] == ["city"]


# ---------------------------------------------------------------------------
# Anthropic — response translation
# ---------------------------------------------------------------------------


def test_anthropic_translate_response_basic():
    anthropic_json = {
        "id": "msg_abc123",
        "type": "message",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": "Hello!"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    envelope = anthropic_provider._translate_response(anthropic_json)
    assert envelope["id"] == "msg_abc123"
    assert envelope["object"] == "chat.completion"
    assert envelope["choices"][0]["message"]["content"] == "Hello!"
    assert envelope["choices"][0]["finish_reason"] == "stop"
    assert envelope["usage"]["prompt_tokens"] == 10
    assert envelope["usage"]["completion_tokens"] == 5
    assert envelope["usage"]["total_tokens"] == 15


def test_anthropic_translate_response_max_tokens_maps_to_length():
    anthropic_json = {
        "id": "msg_1",
        "model": "claude-3",
        "content": [{"type": "text", "text": "partial"}],
        "stop_reason": "max_tokens",
        "usage": {"input_tokens": 5, "output_tokens": 10},
    }
    envelope = anthropic_provider._translate_response(anthropic_json)
    assert envelope["choices"][0]["finish_reason"] == "length"


def test_anthropic_translate_response_includes_tool_calls():
    anthropic_json = {
        "id": "msg_2",
        "model": "claude-3",
        "content": [
            {"type": "text", "text": "Looking up weather"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "get_weather",
                "input": {"city": "NYC"},
            },
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    envelope = anthropic_provider._translate_response(anthropic_json)
    msg = envelope["choices"][0]["message"]
    assert msg["content"] == "Looking up weather"
    assert msg["tool_calls"][0]["id"] == "toolu_1"
    assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"city": "NYC"}
    assert envelope["choices"][0]["finish_reason"] == "tool_calls"


# ---------------------------------------------------------------------------
# Anthropic — end-to-end via httpx mock
# ---------------------------------------------------------------------------


async def test_anthropic_chat_completions_end_to_end(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        json={
            "id": "msg_e2e",
            "type": "message",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "Hi back!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 7, "output_tokens": 3},
        },
    )

    client = anthropic_provider.make_client("test-key")
    try:
        response = await anthropic_provider.chat_completions(
            client,
            {
                "model": "claude-sonnet-4-6",
                "messages": [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                ],
            },
            stream=False,
            extra_headers={},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    envelope = json.loads(response.body)
    assert envelope["choices"][0]["message"]["content"] == "Hi back!"
    assert envelope["usage"]["total_tokens"] == 10

    # Verify the request we sent upstream had auth + translated system message
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    sent = requests[0]
    assert sent.headers.get("x-api-key") == "test-key"
    assert sent.headers.get("anthropic-version") == "2023-06-01"
    sent_body = json.loads(sent.content)
    assert sent_body["system"] == "be terse"
    assert sent_body["messages"] == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# Anthropic — streaming tool_use translation
# ---------------------------------------------------------------------------


async def test_anthropic_stream_tool_use_content_block_start(httpx_mock):
    """content_block_start with tool_use emits OpenAI tool_calls delta chunk with name."""
    sse_lines = [
        'data: {"type": "message_start", "message": {"id": "msg_stream1", "model": "claude-sonnet-4-6"}}',
        'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_abc", "name": "get_weather"}}',
        'data: {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}',
        "data: [DONE]",
    ]
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        content="\n".join(sse_lines).encode(),
        headers={"content-type": "text/event-stream"},
    )

    client = anthropic_provider.make_client("test-key")
    try:
        response = await anthropic_provider.chat_completions(
            client,
            {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "what is the weather?"}],
                "stream": True,
            },
            stream=True,
            extra_headers={},
        )
        chunks = []
        async for chunk_bytes in response.body_iterator:
            text = chunk_bytes if isinstance(chunk_bytes, str) else chunk_bytes.decode()
            for line in text.splitlines():
                if line.startswith("data:") and not line.endswith("[DONE]"):
                    chunks.append(json.loads(line[len("data:"):].strip()))
    finally:
        await client.aclose()

    # Find the tool_calls chunk (content_block_start)
    tool_start_chunks = [
        c for c in chunks
        if c["choices"][0]["delta"].get("tool_calls")
        and c["choices"][0]["delta"]["tool_calls"][0].get("id")
    ]
    assert len(tool_start_chunks) == 1
    tc = tool_start_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["index"] == 0
    assert tc["id"] == "toolu_abc"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert tc["function"]["arguments"] == ""


async def test_anthropic_stream_input_json_delta(httpx_mock):
    """content_block_delta with input_json_delta emits OpenAI tool_calls chunk with partial arguments."""
    sse_lines = [
        'data: {"type": "message_start", "message": {"id": "msg_stream2", "model": "claude-sonnet-4-6"}}',
        'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_xyz", "name": "get_weather"}}',
        'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\\"city\\":"}}',
        'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "\\"NYC\\"}"}}',
        'data: {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}',
        "data: [DONE]",
    ]
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        content="\n".join(sse_lines).encode(),
        headers={"content-type": "text/event-stream"},
    )

    client = anthropic_provider.make_client("test-key")
    try:
        response = await anthropic_provider.chat_completions(
            client,
            {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "weather?"}],
                "stream": True,
            },
            stream=True,
            extra_headers={},
        )
        chunks = []
        async for chunk_bytes in response.body_iterator:
            text = chunk_bytes if isinstance(chunk_bytes, str) else chunk_bytes.decode()
            for line in text.splitlines():
                if line.startswith("data:") and not line.endswith("[DONE]"):
                    chunks.append(json.loads(line[len("data:"):].strip()))
    finally:
        await client.aclose()

    # Find argument delta chunks (have tool_calls but no "id" key — just function.arguments)
    arg_chunks = [
        c for c in chunks
        if c["choices"][0]["delta"].get("tool_calls")
        and "id" not in c["choices"][0]["delta"]["tool_calls"][0]
    ]
    assert len(arg_chunks) == 2
    assert arg_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == '{"city":'
    assert arg_chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == '"NYC"}'
    # Both reference the correct tool_calls index
    assert arg_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0


async def test_anthropic_stream_tool_use_stop_reason_maps_to_tool_calls(httpx_mock):
    """message_delta with stop_reason=tool_use produces finish_reason=tool_calls."""
    sse_lines = [
        'data: {"type": "message_start", "message": {"id": "msg_stream3", "model": "claude-sonnet-4-6"}}',
        'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_fin", "name": "do_thing"}}',
        'data: {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}',
        "data: [DONE]",
    ]
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        content="\n".join(sse_lines).encode(),
        headers={"content-type": "text/event-stream"},
    )

    client = anthropic_provider.make_client("test-key")
    try:
        response = await anthropic_provider.chat_completions(
            client,
            {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "do a thing"}],
                "stream": True,
            },
            stream=True,
            extra_headers={},
        )
        chunks = []
        async for chunk_bytes in response.body_iterator:
            text = chunk_bytes if isinstance(chunk_bytes, str) else chunk_bytes.decode()
            for line in text.splitlines():
                if line.startswith("data:") and not line.endswith("[DONE]"):
                    chunks.append(json.loads(line[len("data:"):].strip()))
    finally:
        await client.aclose()

    # The final chunk (from message_delta) must have finish_reason=tool_calls
    final_chunk = chunks[-1]
    assert final_chunk["choices"][0]["finish_reason"] == "tool_calls"
    assert final_chunk["choices"][0]["delta"] == {}


async def test_anthropic_stream_final_chunk_includes_usage(httpx_mock):
    """message_start input_tokens + message_delta output_tokens are combined
    into a usage block on the final OpenAI-shaped chunk."""
    sse_lines = [
        'data: {"type": "message_start", "message": {"id": "msg_usage1", "model": "claude-sonnet-4-6", "usage": {"input_tokens": 12}}}',
        'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}',
        'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}',
        'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}}',
        "data: [DONE]",
    ]
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        content="\n".join(sse_lines).encode(),
        headers={"content-type": "text/event-stream"},
    )

    client = anthropic_provider.make_client("test-key")
    try:
        response = await anthropic_provider.chat_completions(
            client,
            {
                "model": "claude-sonnet-4-6",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            stream=True,
            extra_headers={},
        )
        chunks = []
        async for chunk_bytes in response.body_iterator:
            text = chunk_bytes if isinstance(chunk_bytes, str) else chunk_bytes.decode()
            for line in text.splitlines():
                if line.startswith("data:") and not line.endswith("[DONE]"):
                    chunks.append(json.loads(line[len("data:"):].strip()))
    finally:
        await client.aclose()

    final_chunk = chunks[-1]
    assert final_chunk["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }


# ---------------------------------------------------------------------------
# Gemini — request translation
# ---------------------------------------------------------------------------


def test_gemini_translate_extracts_system_instruction():
    body = {
        "model": "gemini-3.1-flash-lite",
        "messages": [
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "hi"},
        ],
    }
    model, gemini_body = gemini_provider._translate_request(body)
    assert model == "gemini-3.1-flash-lite"
    assert gemini_body["systemInstruction"] == {
        "parts": [{"text": "You are concise."}]
    }
    assert gemini_body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_gemini_translate_maps_assistant_to_model_role():
    body = {
        "model": "gemini-3.1-flash-lite",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there"},
            {"role": "user", "content": "again"},
        ],
    }
    _, gemini_body = gemini_provider._translate_request(body)
    roles = [c["role"] for c in gemini_body["contents"]]
    assert roles == ["user", "model", "user"]


def test_gemini_translate_generation_config():
    body = {
        "model": "gemini-3.1-flash-lite",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.5,
        "top_p": 0.9,
        "max_tokens": 256,
        "stop": ["END"],
    }
    _, gemini_body = gemini_provider._translate_request(body)
    gc = gemini_body["generationConfig"]
    assert gc["temperature"] == 0.5
    assert gc["topP"] == 0.9
    assert gc["maxOutputTokens"] == 256
    assert gc["stopSequences"] == ["END"]


def test_gemini_translate_preserves_function_tool_loop():
    body = {
        "model": "gemini-3.1-flash-lite",
        "messages": [
            {"role": "user", "content": "look it up"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"key":"x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "value"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Lookup a value",
                    "parameters": {"type": "object"},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    }

    _, gemini_body = gemini_provider._translate_request(body)

    assert gemini_body["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "lookup",
                    "description": "Lookup a value",
                    "parameters": {"type": "object"},
                }
            ]
        }
    ]
    assert gemini_body["contents"][1]["parts"][0]["functionCall"] == {
        "id": "call_1",
        "name": "lookup",
        "args": {"key": "x"},
    }
    assert gemini_body["contents"][2]["parts"][0]["functionResponse"] == {
        "id": "call_1",
        "name": "lookup",
        "response": {"output": "value"},
    }
    assert gemini_body["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["lookup"]}
    }


def test_gemini_translate_rejects_fields_it_cannot_preserve():
    with pytest.raises(gemini_provider.GeminiTranslationError, match="frequency_penalty"):
        gemini_provider._translate_request(
            {
                "model": "gemini-3.1-flash-lite",
                "messages": [{"role": "user", "content": "hi"}],
                "frequency_penalty": 0.5,
            }
        )


def test_gemini_translate_rejects_parallel_tool_calls():
    """parallel_tool_calls has no Gemini equivalent and was never translated
    anywhere in _gemini_common.py — it must be rejected up front rather than
    silently accepted and ignored."""
    with pytest.raises(gemini_provider.GeminiTranslationError, match="parallel_tool_calls"):
        gemini_provider._translate_request(
            {
                "model": "gemini-3.1-flash-lite",
                "messages": [{"role": "user", "content": "hi"}],
                "parallel_tool_calls": False,
            }
        )


# ---------------------------------------------------------------------------
# Gemini — response translation
# ---------------------------------------------------------------------------


def test_gemini_to_openai_envelope_basic():
    gemini_json = {
        "candidates": [
            {
                "content": {"parts": [{"text": "Hi there!"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 4,
            "candidatesTokenCount": 6,
            "totalTokenCount": 10,
        },
    }
    envelope = gemini_provider._to_openai_envelope(gemini_json, "gemini-3.1-flash-lite")
    assert envelope["model"] == "gemini-3.1-flash-lite"
    assert envelope["choices"][0]["message"]["content"] == "Hi there!"
    assert envelope["choices"][0]["finish_reason"] == "stop"
    assert envelope["usage"]["total_tokens"] == 10
    assert envelope["id"].startswith("chatcmpl-gemini-")


def test_gemini_to_openai_envelope_max_tokens_maps_to_length():
    gemini_json = {
        "candidates": [
            {
                "content": {"parts": [{"text": "..."}]},
                "finishReason": "MAX_TOKENS",
            }
        ],
        "usageMetadata": {},
    }
    envelope = gemini_provider._to_openai_envelope(gemini_json, "gemini-3.1-flash-lite")
    assert envelope["choices"][0]["finish_reason"] == "length"


def test_gemini_to_openai_envelope_safety_maps_to_content_filter():
    gemini_json = {
        "candidates": [
            {"content": {"parts": []}, "finishReason": "SAFETY"}
        ],
        "usageMetadata": {},
    }
    envelope = gemini_provider._to_openai_envelope(gemini_json, "gemini-3.1-flash-lite")
    assert envelope["choices"][0]["finish_reason"] == "content_filter"


def test_gemini_to_openai_envelope_rejects_dialect_specific_finish_reason():
    """gemini.py must keep raising on finishReason values outside the core
    map, even though _gemini_common.DEVELOPER_API_DIALECT declares them as
    dialect-legitimate — that acceptance is scoped to future callers, not
    this adapter's existing response path."""
    gemini_json = {
        "candidates": [{"content": {"parts": []}, "finishReason": "LANGUAGE"}],
        "usageMetadata": {},
    }
    with pytest.raises(gemini_provider.GeminiTranslationError, match="LANGUAGE"):
        gemini_provider._to_openai_envelope(gemini_json, "gemini-3.1-flash-lite")


# The raises-on-blocked-prompt / defaults-to-stop-when-unset behavior is
# covered for both this adapter and gemini_vertex.py's in a single shared
# parametrized test — see test_gemini_common.py's
# test_to_openai_envelope_raises_on_blocked_prompt /
# test_to_openai_envelope_default_stop_when_block_reason_unset.
def test_gemini_to_openai_envelope_default_stop_when_prompt_feedback_missing():
    """No promptFeedback at all (e.g. an empty-but-successful response)
    must not be mistaken for a block."""
    gemini_json = {"candidates": [], "usageMetadata": {}}
    envelope = gemini_provider._to_openai_envelope(gemini_json, "gemini-3.1-flash-lite")
    assert envelope["choices"][0]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# Gemini — end-to-end via httpx mock
# ---------------------------------------------------------------------------


async def test_gemini_chat_completions_end_to_end(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent",
        match_headers={"x-goog-api-key": "gemini-test-key"},
        json={
            "candidates": [
                {
                    "content": {"parts": [{"text": "pong"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 2,
                "candidatesTokenCount": 1,
                "totalTokenCount": 3,
            },
        },
    )

    client = gemini_provider.make_client("gemini-test-key")
    try:
        response = await gemini_provider.chat_completions(
            client,
            {
                "model": "gemini-3.1-flash-lite",
                "messages": [{"role": "user", "content": "ping"}],
            },
            stream=False,
            extra_headers={},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    envelope = json.loads(response.body)
    assert envelope["choices"][0]["message"]["content"] == "pong"
    assert envelope["choices"][0]["finish_reason"] == "stop"
    assert envelope["usage"]["total_tokens"] == 3


async def test_gemini_stream_final_chunk_includes_usage(httpx_mock):
    """The last-seen usageMetadata on a streamed chunk is surfaced on the
    final OpenAI-shaped chunk."""
    sse_lines = [
        'data: {"candidates": [{"content": {"parts": [{"text": "pong"}]}}], "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1}}',
        'data: {"candidates": [{"content": {"parts": []}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2}}',
        "data: [DONE]",
    ]
    httpx_mock.add_response(
        method="POST",
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent?alt=sse",
        match_headers={"x-goog-api-key": "gemini-test-key"},
        content="\n".join(sse_lines).encode(),
        headers={"content-type": "text/event-stream"},
    )

    client = gemini_provider.make_client("gemini-test-key")
    try:
        response = await gemini_provider.chat_completions(
            client,
            {
                "model": "gemini-3.1-flash-lite",
                "messages": [{"role": "user", "content": "ping"}],
                "stream": True,
            },
            stream=True,
            extra_headers={},
        )
        chunks = []
        async for chunk_bytes in response.body_iterator:
            text = chunk_bytes if isinstance(chunk_bytes, str) else chunk_bytes.decode()
            for line in text.splitlines():
                if line.startswith("data:") and not line.endswith("[DONE]"):
                    chunks.append(json.loads(line[len("data:"):].strip()))
    finally:
        await client.aclose()

    final_chunk = chunks[-1]
    assert final_chunk["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 2,
        "total_tokens": 4,
    }


async def test_gemini_stream_blocked_prompt_emits_sse_error_frame(httpx_mock):
    """A candidates-less chunk that carries a genuine promptFeedback.block
    Reason must terminate the stream with an SSE error frame, not silently
    complete as a normal empty 'stop' response."""
    sse_lines = [
        'data: {"promptFeedback": {"blockReason": "SAFETY"}}',
        "data: [DONE]",
    ]
    httpx_mock.add_response(
        method="POST",
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent?alt=sse",
        match_headers={"x-goog-api-key": "gemini-test-key"},
        content="\n".join(sse_lines).encode(),
        headers={"content-type": "text/event-stream"},
    )

    client = gemini_provider.make_client("gemini-test-key")
    try:
        response = await gemini_provider.chat_completions(
            client,
            {
                "model": "gemini-3.1-flash-lite",
                "messages": [{"role": "user", "content": "ping"}],
                "stream": True,
            },
            stream=True,
            extra_headers={},
        )
        frames = []
        async for chunk_bytes in response.body_iterator:
            text = chunk_bytes if isinstance(chunk_bytes, str) else chunk_bytes.decode()
            for line in text.splitlines():
                if line.startswith("data:"):
                    frames.append(line[len("data:") :].strip())
    finally:
        await client.aclose()

    assert len(frames) == 1
    error_frame = json.loads(frames[0])
    assert "blocked: SAFETY" in error_frame["error"]["message"]


async def test_gemini_stream_non_dict_part_emits_clean_error_frame_not_attribute_error(
    httpx_mock,
):
    """A malformed `parts` entry (e.g. a bare string instead of an object)
    must be caught by the isinstance(part, dict) guard BEFORE any `.get(...)`
    call is made on it while building `text`. Otherwise AttributeError
    escapes uncaught (no `except` clause in _stream_body() catches it),
    defeating the guard's purpose of turning malformed-shape crashes into a
    clean GeminiTranslationError SSE error frame."""
    sse_lines = [
        'data: {"candidates": [{"content": {"parts": [{"text": "hi"}, "oops"]}}]}',
        "data: [DONE]",
    ]
    httpx_mock.add_response(
        method="POST",
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent?alt=sse",
        match_headers={"x-goog-api-key": "gemini-test-key"},
        content="\n".join(sse_lines).encode(),
        headers={"content-type": "text/event-stream"},
    )

    client = gemini_provider.make_client("gemini-test-key")
    try:
        response = await gemini_provider.chat_completions(
            client,
            {
                "model": "gemini-3.1-flash-lite",
                "messages": [{"role": "user", "content": "ping"}],
                "stream": True,
            },
            stream=True,
            extra_headers={},
        )
        frames = []
        async for chunk_bytes in response.body_iterator:
            text = chunk_bytes if isinstance(chunk_bytes, str) else chunk_bytes.decode()
            for line in text.splitlines():
                if line.startswith("data:"):
                    frames.append(line[len("data:") :].strip())
    finally:
        await client.aclose()

    assert len(frames) == 1
    error_frame = json.loads(frames[0])
    assert error_frame["error"]["type"] == "provider_response_error"
    assert "part 1 must be an object" in error_frame["error"]["message"]


# ---------------------------------------------------------------------------
# Ollama — pass-through end-to-end
# ---------------------------------------------------------------------------


async def test_ollama_chat_completions_passes_through(httpx_mock):
    upstream_body = {
        "id": "chatcmpl-ollama-1",
        "object": "chat.completion",
        "model": "llama3",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hi from llama"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:11434/v1/chat/completions",
        json=upstream_body,
    )

    client = ollama_provider.make_client("http://localhost:11434/v1")
    try:
        response = await ollama_provider.chat_completions(
            client,
            {"model": "llama3", "messages": [{"role": "user", "content": "hi"}]},
            stream=False,
            extra_headers={},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert json.loads(response.body) == upstream_body

    # Ollama must not send Authorization header
    sent = httpx_mock.get_requests()[0]
    assert "authorization" not in {k.lower() for k in sent.headers}


# ---------------------------------------------------------------------------
# Generic — base_url + optional auth
# ---------------------------------------------------------------------------


async def test_generic_chat_completions_with_api_key_sends_bearer(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://example.com/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    )

    response = await generic_provider.chat_completions(
        {"model": "custom-llm", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
        extra_headers={},
        base_url="https://example.com/v1",
        api_key="secret-token",
    )

    assert response.status_code == 200
    sent = httpx_mock.get_requests()[0]
    assert sent.headers.get("authorization") == "Bearer secret-token"


async def test_generic_chat_completions_without_api_key_omits_auth(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://example.com/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    )

    response = await generic_provider.chat_completions(
        {"model": "custom-llm", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
        extra_headers={},
        base_url="https://example.com/v1/",  # trailing slash should be stripped
        api_key="",
    )

    assert response.status_code == 200
    sent = httpx_mock.get_requests()[0]
    assert "authorization" not in {k.lower() for k in sent.headers}
    # Trailing slash on base_url is normalised
    assert str(sent.url) == "https://example.com/v1/chat/completions"


async def test_generic_propagates_upstream_status_code(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://example.com/v1/chat/completions",
        status_code=502,
        content=b'{"error":"upstream"}',
    )

    response = await generic_provider.chat_completions(
        {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        stream=False,
        extra_headers={},
        base_url="https://example.com/v1",
        api_key="",
    )

    assert response.status_code == 502


# ---------------------------------------------------------------------------
# Generic — per-base_url client pool
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def clear_generic_pool():
    """Reset the generic adapter's client pool before and after each test."""
    await generic_provider.close_all_clients()
    yield
    await generic_provider.close_all_clients()


async def test_generic_pool_same_base_url_returns_same_client():
    """Two calls with identical base_url must return the same client instance."""
    origin = "https://pool-test.example.com"
    client_a = await generic_provider.get_pooled_client(origin)
    client_b = await generic_provider.get_pooled_client(origin)
    assert client_a is client_b


async def test_generic_pool_different_base_urls_return_different_clients():
    """Two calls with different base_urls must return distinct client instances."""
    client_a = await generic_provider.get_pooled_client("https://host-a.example.com")
    client_b = await generic_provider.get_pooled_client("https://host-b.example.com")
    assert client_a is not client_b


async def test_generic_pool_client_has_correct_base_url():
    """Pooled client must have its base_url set to the origin passed in."""
    origin = "https://myapi.example.com"
    client = await generic_provider.get_pooled_client(origin)
    # httpx normalises the base_url — compare without trailing slash
    assert str(client.base_url).rstrip("/") == origin


async def test_generic_pool_extract_origin_strips_path():
    """_extract_origin must strip the path component, keeping only scheme+host."""
    assert generic_provider._extract_origin("https://api.example.com/v1") == "https://api.example.com"
    assert generic_provider._extract_origin("https://api.example.com:8443/v1") == "https://api.example.com:8443"
    assert generic_provider._extract_origin("http://localhost:11434/api") == "http://localhost:11434"


# ---------------------------------------------------------------------------
# Error sanitization — shared utility (proxy.app.providers.errors)
# ---------------------------------------------------------------------------


from proxy.app.providers.errors import _extract_message, sanitize_upstream_error  # noqa: E402


def _make_upstream(status_code: int, body: bytes, content_type: str = "application/json") -> object:
    """Build a minimal fake httpx.Response for sanitize_upstream_error tests."""
    import httpx

    return httpx.Response(
        status_code=status_code,
        content=body,
        headers={"content-type": content_type},
    )


def test_sanitize_401_returns_authentication_error():
    upstream = _make_upstream(401, b'{"error": {"message": "Invalid API key."}}')
    resp = sanitize_upstream_error(upstream, provider="openai")
    assert resp.status_code == 401
    body = json.loads(resp.body)
    assert body["error"]["type"] == "authentication_error"
    # Raw internal details must NOT be present
    assert "Invalid API key." not in resp.body.decode() or body["error"]["message"] == "Invalid API key."
    # Content-Type must always be application/json
    assert resp.media_type == "application/json"


def test_sanitize_429_returns_rate_limit_error():
    upstream = _make_upstream(429, b'{"error": {"message": "Too many requests."}}')
    resp = sanitize_upstream_error(upstream, provider="openai")
    assert resp.status_code == 429
    body = json.loads(resp.body)
    assert body["error"]["type"] == "rate_limit_error"
    assert body["error"]["code"] == "429"


def test_sanitize_400_returns_invalid_request_error():
    upstream = _make_upstream(400, b'{"error": {"message": "Bad request."}}')
    resp = sanitize_upstream_error(upstream, provider="openai")
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["error"]["type"] == "invalid_request_error"


def test_sanitize_500_returns_api_error():
    upstream = _make_upstream(
        500,
        b'{"error": {"message": "An internal server error occurred."}}',
    )
    resp = sanitize_upstream_error(upstream, provider="openai")
    assert resp.status_code == 500
    body = json.loads(resp.body)
    assert body["error"]["type"] == "api_error"
    # Response is always valid JSON with the normalized envelope shape
    assert "error" in body
    assert body["error"]["code"] == "500"
    assert resp.media_type == "application/json"


def test_sanitize_unknown_status_returns_api_error():
    upstream = _make_upstream(418, b'{"error": {"message": "I am a teapot."}}')
    resp = sanitize_upstream_error(upstream, provider="openai")
    assert resp.status_code == 418
    body = json.loads(resp.body)
    assert body["error"]["type"] == "api_error"


def test_sanitize_json_upstream_extracts_message_for_400():
    # 400 is not an opaque status — the upstream message is extracted.
    upstream = _make_upstream(
        400,
        b'{"error": {"message": "Invalid model specified.", "type": "invalid_request_error"}}',
    )
    resp = sanitize_upstream_error(upstream, provider="openai")
    body = json.loads(resp.body)
    assert body["error"]["message"] == "Invalid model specified."


def test_sanitize_401_always_returns_generic_message():
    # Auth errors use the generic message to avoid leaking credential hints.
    upstream = _make_upstream(
        401,
        b'{"error": {"message": "Incorrect API key provided.", "type": "invalid_api_key"}}',
    )
    resp = sanitize_upstream_error(upstream, provider="openai")
    body = json.loads(resp.body)
    assert "Incorrect API key" not in body["error"]["message"]
    assert body["error"]["type"] == "authentication_error"


def test_sanitize_non_json_upstream_returns_generic_message():
    upstream = _make_upstream(503, b"Service Unavailable", content_type="text/plain")
    resp = sanitize_upstream_error(upstream, provider="openai")
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["error"]["type"] == "api_error"
    # Message is a generic fallback, not raw bytes
    assert "Service Unavailable" not in body["error"]["message"]
    assert body["error"]["message"]  # non-empty


def test_sanitize_raw_body_not_forwarded_verbatim():
    """The raw upstream body must not be forwarded as-is — only the normalized envelope is returned."""
    raw = b'{"error": {"message": "something went wrong"}, "extra_internal_field": "secret-value"}'
    upstream = _make_upstream(500, raw)
    resp = sanitize_upstream_error(upstream, provider="anthropic")
    body = json.loads(resp.body)
    # Response must be the normalized envelope shape, not the raw upstream body
    assert set(body["error"].keys()) == {"message", "type", "code"}
    # Extra internal fields from the upstream must not leak into the envelope
    assert "extra_internal_field" not in resp.body.decode()
    assert "secret-value" not in resp.body.decode()


def test_extract_message_openai_nested():
    assert _extract_message(b'{"error": {"message": "hello"}}') == "hello"


def test_extract_message_flat_detail():
    assert _extract_message(b'{"detail": "not found"}') == "not found"


def test_extract_message_invalid_json_returns_generic():
    msg = _extract_message(b"not json at all")
    assert msg  # non-empty
    assert "upstream" in msg.lower() or "error" in msg.lower()


def test_sanitize_classify_does_not_alter_envelope_shape():
    """The optional `classify` kwarg is for internal logging only — the
    caller-facing envelope shape/content must be identical with or without it."""
    upstream = _make_upstream(403, b'{"error": {"message": "denied"}}')
    resp_without = sanitize_upstream_error(upstream, provider="gemini-vertex")
    resp_with = sanitize_upstream_error(
        upstream, provider="gemini-vertex", classify=lambda _status, _body: "some_internal_label"
    )
    assert json.loads(resp_without.body) == json.loads(resp_with.body)
    assert resp_without.status_code == resp_with.status_code
    assert "some_internal_label" not in resp_with.body.decode()


def test_sanitize_response_always_application_json():
    """Content-Type must always be application/json regardless of upstream type."""
    upstream = _make_upstream(500, b"<html>Error</html>", content_type="text/html")
    resp = sanitize_upstream_error(upstream, provider="gemini")
    assert resp.media_type == "application/json"
    # Body must be valid JSON
    body = json.loads(resp.body)
    assert "error" in body


# ---------------------------------------------------------------------------
# anthropic_compat — messages_to_chat_body tool-use translation
# ---------------------------------------------------------------------------


def _messages_request(**overrides) -> AnthropicMessagesRequest:
    payload = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 100,
    }
    payload.update(overrides)
    return AnthropicMessagesRequest.model_validate(payload)


def test_messages_to_chat_body_translates_tools_and_tool_choice():
    req = _messages_request(
        tools=[
            {
                "name": "get_weather",
                "description": "Get the weather",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ],
        tool_choice={"type": "tool", "name": "get_weather"},
    )
    body = messages_to_chat_body(req)
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    assert body["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}


def test_messages_to_chat_body_tool_choice_auto_and_any():
    auto_body = messages_to_chat_body(_messages_request(tool_choice={"type": "auto"}))
    assert auto_body["tool_choice"] == "auto"
    any_body = messages_to_chat_body(
        _messages_request(
            tool_choice={"type": "any"},
            tools=[{"name": "lookup", "input_schema": {"type": "object"}}],
        )
    )
    assert any_body["tool_choice"] == "required"
    none_body = messages_to_chat_body(_messages_request(tool_choice={"type": "none"}))
    assert none_body["tool_choice"] == "none"


def test_messages_to_chat_body_translates_multiturn_tool_use_and_tool_result():
    req = _messages_request(
        messages=[
            {"role": "user", "content": "What's the weather in NYC?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {"city": "NYC"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "72F and sunny"}
                ],
            },
        ],
    )
    body = messages_to_chat_body(req)
    msgs = body["messages"]
    assert msgs[0] == {"role": "user", "content": "What's the weather in NYC?"}

    assistant_msg = msgs[1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] == "Let me check."
    assert assistant_msg["tool_calls"] == [
        {
            "id": "toolu_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": "NYC"})},
        }
    ]

    tool_msg = msgs[2]
    assert tool_msg == {"role": "tool", "tool_call_id": "toolu_1", "content": "72F and sunny"}


def test_messages_to_chat_body_rejects_tool_result_error_semantics():
    req = _messages_request(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "not found",
                        "is_error": True,
                    }
                ],
            },
        ],
    )
    with pytest.raises(AnthropicCompatError, match="is_error=true"):
        messages_to_chat_body(req)


def _tool_result_image_message() -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "AA==",
                        },
                    }
                ],
            }
        ],
    }


def test_messages_request_accepts_tool_result_image_content():
    # A tool (e.g. a screenshot tool) can return an image in its tool_result,
    # per the real Anthropic Messages API. Ingress must accept this shape so a
    # native Anthropic-to-Anthropic request can pass through unchanged — only
    # Chat Completions translation (which cannot represent an image in a
    # tool-role message) may reject it.
    req = _messages_request(messages=[_tool_result_image_message()])
    block = req.messages[0].content[0]
    assert isinstance(block, AnthropicToolResultBlock)
    assert isinstance(block.content[0], AnthropicImageBlock)


def test_messages_request_native_body_round_trips_tool_result_image_content():
    req = _messages_request(messages=[_tool_result_image_message()])
    native = req.model_dump(mode="json", by_alias=True, exclude_none=True)
    tool_result = native["messages"][0]["content"][0]
    assert tool_result["content"][0]["type"] == "image"
    assert tool_result["content"][0]["source"]["data"] == "AA=="


def test_messages_to_chat_body_rejects_tool_result_image_content():
    req = _messages_request(messages=[_tool_result_image_message()])
    with pytest.raises(AnthropicCompatError, match="image content"):
        messages_to_chat_body(req)


def test_with_redacted_text_writes_trailing_share_into_none_content_tool_result():
    # redistribute_redacted_text() routes any redacted text past the end of
    # all collected leaves to the LAST leaf. When that last leaf is a
    # tool_result block whose `content` is None, `_redact_tool_result_content`
    # must still write the trailing share instead of silently dropping it
    # via an early `return`.
    req = _messages_request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "short"},
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": None},
                ],
            }
        ],
    )
    payload = AnthropicGatewayPayload(request=req)

    redacted = payload.with_redacted_text("short PLUS EXTRA TRAILING TEXT")

    content = redacted.request.messages[0].content
    text_block, tool_result_block = content
    assert isinstance(text_block, AnthropicTextBlock)
    assert isinstance(tool_result_block, AnthropicToolResultBlock)
    assert text_block.text == "short"
    assert tool_result_block.content == " PLUS EXTRA TRAILING TEXT"


def test_messages_to_chat_body_system_content_block_array_flattened_to_text():
    req = _messages_request(
        system=[
            {"type": "text", "text": "Be concise.", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": " Be kind."},
        ],
    )
    body = messages_to_chat_body(req)
    assert body["messages"][0] == {"role": "system", "content": "Be concise. Be kind."}


def test_messages_to_chat_body_rejects_unsupported_content_block():
    # A well-formed image block (valid per AnthropicImageBlock.source's
    # discriminated union) is still an unsupported content *type* for Chat
    # Completions translation — this must be rejected by messages_to_chat_body
    # itself, not by request parsing.
    req = _messages_request(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "abc123",
                        },
                    }
                ],
            }
        ],
    )
    with pytest.raises(AnthropicCompatError, match="Unsupported content block type"):
        messages_to_chat_body(req)


# ---------------------------------------------------------------------------
# anthropic_compat — openai_sse_to_anthropic_sse streaming translation
# ---------------------------------------------------------------------------


async def _sse_body(*raw_chunks: str | bytes):
    for chunk in raw_chunks:
        yield chunk


def _openai_chunk_sse(
    choices: list[dict],
    *,
    chunk_id: str,
    usage: dict | None = None,
) -> str:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "translated-model",
        "choices": choices,
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_lines(events: str) -> list[dict]:
    """Parse `event: ...` / `data: ...` pairs out of the emitted SSE text."""
    parsed = []
    for block in events.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        data_line = next((line for line in block.splitlines() if line.startswith("data:")), None)
        if data_line:
            parsed.append(json.loads(data_line[len("data:"):].strip()))
    return parsed


async def test_openai_sse_to_anthropic_sse_preserves_message_id_and_usage():
    chunks = [
        _openai_chunk_sse(
            [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": None}],
            chunk_id="chatcmpl-real-id",
        ),
        _openai_chunk_sse(
            [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            chunk_id="chatcmpl-real-id",
        ),
        _openai_chunk_sse(
            [],
            chunk_id="chatcmpl-real-id",
            usage={"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10},
        ),
        "data: [DONE]\n\n",
    ]
    events = [chunk async for chunk in openai_sse_to_anthropic_sse(_sse_body(*chunks), "claude-sonnet-4-6")]
    full_text = "".join(events)
    parsed = _sse_lines(full_text)
    assert parsed[0]["type"] == "message_start"
    assert parsed[0]["message"]["id"] == "chatcmpl-real-id"
    message_delta = next(e for e in parsed if e["type"] == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert message_delta["usage"]["output_tokens"] == 7
    assert parsed[-1]["type"] == "message_stop"


async def test_openai_sse_to_anthropic_sse_translates_tool_call_deltas():
    chunks = [
        _openai_chunk_sse(
            [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "",
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
            chunk_id="chatcmpl-1",
        ),
        _openai_chunk_sse(
            [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"city":'}}
                        ]
                    },
                    "finish_reason": None,
                }
            ],
            chunk_id="chatcmpl-1",
        ),
        _openai_chunk_sse(
            [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"NYC"}'}}
                        ]
                    },
                    "finish_reason": None,
                }
            ],
            chunk_id="chatcmpl-1",
        ),
        _openai_chunk_sse(
            [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            chunk_id="chatcmpl-1",
        ),
        "data: [DONE]\n\n",
    ]
    events = [chunk async for chunk in openai_sse_to_anthropic_sse(_sse_body(*chunks), "claude-sonnet-4-6")]
    full_text = "".join(events)
    parsed = _sse_lines(full_text)

    tool_start = next(e for e in parsed if e["type"] == "content_block_start" and e["index"] == 1)
    assert tool_start["content_block"]["type"] == "tool_use"
    assert tool_start["content_block"]["id"] == "call_1"
    assert tool_start["content_block"]["name"] == "get_weather"

    json_deltas = [
        e for e in parsed if e["type"] == "content_block_delta" and e["index"] == 1
    ]
    combined_json = "".join(d["delta"]["partial_json"] for d in json_deltas)
    assert combined_json == '{"city":"NYC"}'

    message_delta = next(e for e in parsed if e["type"] == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "tool_use"

    stop_indices = {e["index"] for e in parsed if e["type"] == "content_block_stop"}
    assert stop_indices == {0, 1}


async def test_openai_sse_to_anthropic_sse_surfaces_mid_stream_error():
    chunks = [
        _openai_chunk_sse(
            [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
            chunk_id="chatcmpl-1",
        ),
        'data: {"error": {"type": "upstream_timeout"}}\n\n',
    ]
    events = [chunk async for chunk in openai_sse_to_anthropic_sse(_sse_body(*chunks), "claude-sonnet-4-6")]
    full_text = "".join(events)
    assert "event: error" in full_text
    parsed = _sse_lines(full_text)
    error_event = next(e for e in parsed if e["type"] == "error")
    assert error_event["error"]["type"] == "upstream_timeout"
    # No message_stop should follow a surfaced mid-stream error.
    assert not any(e["type"] == "message_stop" for e in parsed)


async def test_openai_sse_to_anthropic_sse_buffers_fragmented_chunks():
    """A single data: line split across two network chunks must still parse correctly."""
    full_line = (
        _openai_chunk_sse(
            [{"index": 0, "delta": {"content": "Hello world"}, "finish_reason": None}],
            chunk_id="chatcmpl-frag",
        )
    )
    split_at = len(full_line) // 2
    chunks = [
        full_line[:split_at],
        full_line[split_at:],
        _openai_chunk_sse(
            [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            chunk_id="chatcmpl-frag",
        ),
    ]
    events = [chunk async for chunk in openai_sse_to_anthropic_sse(_sse_body(*chunks), "claude-sonnet-4-6")]
    full_text = "".join(events)
    parsed = _sse_lines(full_text)
    text_delta = next(e for e in parsed if e["type"] == "content_block_delta" and e["index"] == 0)
    assert text_delta["delta"]["text"] == "Hello world"


async def test_openai_sse_to_anthropic_sse_preserves_split_multibyte_utf8():
    line = _openai_chunk_sse(
        [{"index": 0, "delta": {"content": "hello 🌿"}, "finish_reason": None}],
        chunk_id="chatcmpl-utf8",
    ).encode()
    split_at = line.index("🌿".encode()) + 1
    chunks = [
        line[:split_at],
        line[split_at:],
        _openai_chunk_sse(
            [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            chunk_id="chatcmpl-utf8",
        ).encode(),
    ]

    events = [
        chunk
        async for chunk in openai_sse_to_anthropic_sse(
            _sse_body(*chunks), "claude-sonnet-4-6"
        )
    ]
    parsed = _sse_lines("".join(events))

    text_delta = next(e for e in parsed if e["type"] == "content_block_delta")
    assert text_delta["delta"]["text"] == "hello 🌿"


async def test_openai_sse_to_anthropic_sse_rejects_malformed_json():
    events = [
        chunk
        async for chunk in openai_sse_to_anthropic_sse(
            _sse_body('data: {"choices": [}\n\n'), "claude-sonnet-4-6"
        )
    ]
    parsed = _sse_lines("".join(events))

    assert [event["type"] for event in parsed] == ["error"]
    assert parsed[0]["error"]["type"] == "invalid_upstream_stream"


async def test_openai_sse_to_anthropic_sse_rejects_incomplete_stream():
    chunks = [
        _openai_chunk_sse(
            [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
            chunk_id="chatcmpl-incomplete",
        ),
        "data: [DONE]\n\n",
    ]
    events = [
        chunk
        async for chunk in openai_sse_to_anthropic_sse(
            _sse_body(*chunks), "claude-sonnet-4-6"
        )
    ]
    parsed = _sse_lines("".join(events))

    assert parsed[-1]["type"] == "error"
    assert parsed[-1]["error"]["type"] == "incomplete_upstream_stream"
    assert not any(event["type"] == "message_stop" for event in parsed)


async def test_openai_sse_to_anthropic_sse_rejects_missing_initial_tool_identity():
    chunks = [
        _openai_chunk_sse(
            [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"city":"Camas"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            chunk_id="chatcmpl-bad-tool",
        )
    ]
    events = [
        chunk
        async for chunk in openai_sse_to_anthropic_sse(
            _sse_body(*chunks), "claude-sonnet-4-6"
        )
    ]
    parsed = _sse_lines("".join(events))

    assert parsed[-1]["type"] == "error"
    assert parsed[-1]["error"]["type"] == "invalid_upstream_stream"
    assert "toolu_compat" not in "".join(events)
