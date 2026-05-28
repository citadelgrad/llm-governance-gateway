from __future__ import annotations

import json

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

    client = generic_provider.make_client()
    try:
        response = await generic_provider.chat_completions(
            client,
            {"model": "custom-llm", "messages": [{"role": "user", "content": "hi"}]},
            stream=False,
            extra_headers={},
            base_url="https://example.com/v1",
            api_key="secret-token",
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    sent = httpx_mock.get_requests()[0]
    assert sent.headers.get("authorization") == "Bearer secret-token"


async def test_generic_chat_completions_without_api_key_omits_auth(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://example.com/v1/chat/completions",
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    )

    client = generic_provider.make_client()
    try:
        response = await generic_provider.chat_completions(
            client,
            {"model": "custom-llm", "messages": [{"role": "user", "content": "hi"}]},
            stream=False,
            extra_headers={},
            base_url="https://example.com/v1/",  # trailing slash should be stripped
            api_key="",
        )
    finally:
        await client.aclose()

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

    client = generic_provider.make_client()
    try:
        response = await generic_provider.chat_completions(
            client,
            {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
            stream=False,
            extra_headers={},
            base_url="https://example.com/v1",
            api_key="",
        )
    finally:
        await client.aclose()

    assert response.status_code == 502
