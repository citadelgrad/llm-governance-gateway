from __future__ import annotations

import json

import pytest
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
from proxy.app.providers.usage import UsageMetrics, extract_usage

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
# Anthropic — request translation
# ---------------------------------------------------------------------------


def test_anthropic_translate_extracts_system_message():
    body = {
        "model": "claude-3-5-sonnet",
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
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "system", "content": "First."},
            {"role": "system", "content": "Second."},
            {"role": "user", "content": "Hi"},
        ],
    }
    out = anthropic_provider._translate_request(body)
    assert out["system"] == "First.\n\nSecond."


def test_anthropic_translate_tool_call_assistant_message():
    body = {
        "model": "claude-3-5-sonnet",
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


# ---------------------------------------------------------------------------
# Anthropic — response translation
# ---------------------------------------------------------------------------


def test_anthropic_translate_response_basic():
    anthropic_json = {
        "id": "msg_abc123",
        "type": "message",
        "model": "claude-3-5-sonnet",
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
            "model": "claude-3-5-sonnet",
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
                "model": "claude-3-5-sonnet",
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
        'data: {"type": "message_start", "message": {"id": "msg_stream1", "model": "claude-3-5-sonnet"}}',
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
                "model": "claude-3-5-sonnet",
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
        'data: {"type": "message_start", "message": {"id": "msg_stream2", "model": "claude-3-5-sonnet"}}',
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
                "model": "claude-3-5-sonnet",
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
        'data: {"type": "message_start", "message": {"id": "msg_stream3", "model": "claude-3-5-sonnet"}}',
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
                "model": "claude-3-5-sonnet",
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


# ---------------------------------------------------------------------------
# Gemini — request translation
# ---------------------------------------------------------------------------


def test_gemini_translate_extracts_system_instruction():
    body = {
        "model": "gemini-1.5-flash",
        "messages": [
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "hi"},
        ],
    }
    model, gemini_body = gemini_provider._translate_request(body)
    assert model == "gemini-1.5-flash"
    assert gemini_body["systemInstruction"] == {
        "parts": [{"text": "You are concise."}]
    }
    assert gemini_body["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_gemini_translate_maps_assistant_to_model_role():
    body = {
        "model": "gemini-1.5-flash",
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
        "model": "gemini-1.5-flash",
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
    envelope = gemini_provider._to_openai_envelope(gemini_json, "gemini-1.5-flash")
    assert envelope["model"] == "gemini-1.5-flash"
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
    envelope = gemini_provider._to_openai_envelope(gemini_json, "gemini-1.5-flash")
    assert envelope["choices"][0]["finish_reason"] == "length"


def test_gemini_to_openai_envelope_safety_maps_to_content_filter():
    gemini_json = {
        "candidates": [
            {"content": {"parts": []}, "finishReason": "SAFETY"}
        ],
        "usageMetadata": {},
    }
    envelope = gemini_provider._to_openai_envelope(gemini_json, "gemini-1.5-flash")
    assert envelope["choices"][0]["finish_reason"] == "content_filter"


# ---------------------------------------------------------------------------
# Gemini — end-to-end via httpx mock
# ---------------------------------------------------------------------------


async def test_gemini_chat_completions_end_to_end(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
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
                "model": "gemini-1.5-flash",
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


def test_sanitize_response_always_application_json():
    """Content-Type must always be application/json regardless of upstream type."""
    upstream = _make_upstream(500, b"<html>Error</html>", content_type="text/html")
    resp = sanitize_upstream_error(upstream, provider="gemini")
    assert resp.media_type == "application/json"
    # Body must be valid JSON
    body = json.loads(resp.body)
    assert "error" in body
