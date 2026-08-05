from __future__ import annotations

import json
from unittest.mock import AsyncMock

from proxy.app.config import settings
from proxy.app.governance_client import InspectResponse
from proxy.app.main import app
from proxy.app.providers import openai as openai_provider
from proxy.app.responses_compat import (
    openai_sse_to_responses_sse,
    translate_chat_response,
    translate_responses_request,
)
from starlette.responses import Response

_RESPONSES_BODY = {
    "model": "gpt-5.6-luna",
    "input": "Reply with gateway-ok only.",
}


async def _sse_chunks(*chunks: bytes):
    for chunk in chunks:
        yield chunk


async def _responses_events(*chunks: bytes) -> list[dict]:
    frames = [
        frame
        async for frame in openai_sse_to_responses_sse(
            _sse_chunks(*chunks), "translated-model"
        )
    ]
    return [json.loads(frame.removeprefix("data: ").strip()) for frame in frames]


def _chat_sse(payload: dict) -> bytes:
    event = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "translated-model",
        **payload,
    }
    return f"data: {json.dumps(event)}\n\n".encode()


def test_chat_tool_call_response_preserves_responses_call_identity():
    response = translate_chat_response(
        {
            "id": "chatcmpl_1",
            "created": 1,
            "model": "translated-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_weather_1",
                                "type": "function",
                                "function": {
                                    "name": "weather",
                                    "arguments": '{"city":"Portland"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
    )

    assert response["output"] == [
        {
            "id": "fc_call_weather_1",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_weather_1",
            "name": "weather",
            "arguments": '{"city":"Portland"}',
        }
    ]


async def test_chat_tool_stream_preserves_arguments_usage_and_terminal_order():
    events = await _responses_events(
        _chat_sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "weather", "arguments": '{"city":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _chat_sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '"Portland"}'}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ),
        _chat_sse(
            {
                "choices": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }
        ),
        b"data: [DONE]\n\n",
    )

    event_types = [event["type"] for event in events]
    assert event_types == [
        "response.created",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ]
    completed = events[-1]["response"]
    assert completed["output"][0]["call_id"] == "call_1"
    assert completed["output"][0]["name"] == "weather"
    assert completed["output"][0]["arguments"] == '{"city":"Portland"}'
    assert completed["usage"]["total_tokens"] == 8


async def test_chat_stream_error_emits_failed_terminal_event():
    events = await _responses_events(
        b'data: {"error":{"type":"upstream_error","message":"generation failed"}}\n\n'
    )

    assert [event["type"] for event in events] == ["response.created", "response.failed"]
    assert events[-1]["response"]["error"] == {
        "type": "upstream_error",
        "message": "generation failed",
    }


async def test_chat_stream_malformed_json_emits_failed_terminal_event():
    events = await _responses_events(b'data: {"choices": [}\n\n')

    assert [event["type"] for event in events] == ["response.created", "response.failed"]
    assert events[-1]["response"]["error"]["type"] == "invalid_upstream_stream"


async def test_chat_stream_missing_terminal_reason_preserves_partial_text_in_failure():
    events = await _responses_events(
        _chat_sse(
            {
                "choices": [
                    {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
                ]
            }
        ),
        b"data: [DONE]\n\n",
    )

    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["output_text"] == "partial"
    assert events[-1]["response"]["output"][0]["status"] == "incomplete"


async def test_chat_stream_rejects_missing_initial_tool_identity():
    events = await _responses_events(
        _chat_sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"city":"Camas"}'}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
    )

    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["error"]["type"] == "invalid_upstream_stream"
    assert events[-1]["response"]["output"] == []


async def test_chat_stream_rejects_unknown_finish_reason():
    events = await _responses_events(
        _chat_sse(
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "provider_magic"}
                ]
            }
        )
    )

    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["error"]["type"] == "invalid_upstream_stream"


async def test_chat_stream_rejects_negative_tool_index():
    events = await _responses_events(
        _chat_sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": -1,
                                    "id": "call_bad",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
    )

    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["error"]["type"] == "invalid_upstream_stream"


async def test_responses_accepts_bearer_api_key(auth_client, api_key_creds):
    client, pool = auth_client
    raw_key, key_hash = api_key_creds
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchrow.side_effect = [
        {"hash": key_hash, "user_id": "key-user", "tenant_id": "test-tenant", "roles": ["user"]},
        {
            "default_provider": "openai",
            "allowed_models": ["gpt-5.6-luna"],
            "pii_redaction_notification": "header",
            "rate_limit_requests_per_minute": 100,
        },
    ]

    response = await client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {raw_key}"},
        json=_RESPONSES_BODY,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["output_text"]


async def test_responses_rejects_model_not_allowed(async_client):
    client, _ = async_client
    conn = app.state.db_pool.acquire.return_value.__aenter__.return_value
    conn.fetchrow.return_value = {
        "default_provider": "openai",
        "allowed_models": ["gpt-4o"],
        "pii_redaction_notification": "header",
        "rate_limit_requests_per_minute": 100,
    }

    response = await client.post("/v1/responses", json=_RESPONSES_BODY)

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "model_not_allowed"


async def test_responses_translate_happy_path(async_client):
    client, _ = async_client

    response = await client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.6-luna",
            "instructions": "Be terse.",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Reply with gateway-ok only."}],
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "response"
    assert body["model"] == "gpt-5.6-luna"
    assert body["status"] == "completed"
    assert body["output_text"]
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["content"][0]["type"] == "output_text"
    assert body["usage"]["input_tokens"] == 10
    assert body["usage"]["output_tokens"] == 5
    assert response.headers.get("x-audit-id") == "test-audit-id"
    assert response.headers.get("x-usage-total-tokens") == "15"


async def test_responses_pii_redaction_headers(async_client, gov_mock):
    client, _ = async_client
    gov_mock.inspect.return_value = InspectResponse(
        decision="allow",
        redacted_text="My SSN is [REDACTED]",
        pii_findings=[{"type": "SSN", "start": 10, "end": 21}],
        harm_score=0.0,
        violations=[],
        audit_id="responses-pii-audit-id",
    )

    response = await client.post(
        "/v1/responses",
        json={"model": "gpt-5.6-luna", "input": "My SSN is 123-45-6789"},
    )

    assert response.status_code == 200
    assert response.headers.get("x-gateway-pii-redacted") == "true"
    assert "SSN" in response.headers.get("x-gateway-pii-types", "")
    assert response.headers.get("x-audit-id") == "responses-pii-audit-id"


async def test_responses_governance_block(async_client, gov_mock):
    client, _ = async_client
    gov_mock.inspect.return_value = InspectResponse(
        decision="block",
        redacted_text="",
        pii_findings=[],
        harm_score=0.9,
        violations=["policy:data_classification_mismatch"],
        audit_id="responses-block-audit-id",
    )

    response = await client.post("/v1/responses", json=_RESPONSES_BODY)

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "policy_violation"
    assert response.headers.get("x-audit-id") == "responses-block-audit-id"


async def test_responses_rejects_unsupported_shape(async_client):
    client, _ = async_client

    response = await client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.6-luna",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "text": "not-supported"}],
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "unsupported_response_shape"


def test_responses_translation_preserves_text_part_boundaries():
    body = translate_responses_request(
        {
            "model": "gpt-5.6-luna",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "line1\n"},
                        {"type": "input_text", "text": "line2"},
                    ],
                }
            ],
        }
    )
    assert body["messages"][-1]["content"] == "line1\nline2"


def test_responses_translation_forwards_generation_options():
    body = translate_responses_request(
        {
            "model": "gpt-5.6-luna",
            "input": "hello",
            "max_output_tokens": 123,
            "temperature": 0.2,
            "top_p": 0.9,
        }
    )
    assert body["max_tokens"] == 123
    assert body["temperature"] == 0.2
    assert body["top_p"] == 0.9


async def test_responses_rejects_conflicting_lifecycle_state(async_client):
    client, _ = async_client
    response = await client.post(
        "/v1/responses",
        json={
            **_RESPONSES_BODY,
            "previous_response_id": "resp_previous",
            "conversation": "conv_123",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "invalid_request"


async def test_responses_rejects_stream_options_without_stream(async_client):
    client, _ = async_client
    response = await client.post(
        "/v1/responses",
        json={**_RESPONSES_BODY, "stream_options": {"include_obfuscation": True}},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "invalid_request"


async def test_responses_rejects_tools_instead_of_silently_dropping(async_client):
    client, _ = async_client
    response = await client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.6-luna",
            "input": "hello",
            "tools": [{"type": "function", "name": "lookup"}],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "unsupported_response_shape"


async def test_responses_uses_lossless_native_openai_dispatch(async_client, monkeypatch):
    client, _ = async_client
    native_responses = AsyncMock(
        return_value=Response(
            content=b'{"id":"resp_native","object":"response","status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}',
            status_code=200,
            media_type="application/json",
        )
    )
    translated_chat = AsyncMock(side_effect=AssertionError("chat translation must not run"))
    monkeypatch.setattr(settings, "mock_mode", False)
    monkeypatch.setattr(openai_provider, "responses", native_responses)
    monkeypatch.setattr(openai_provider, "chat_completions", translated_chat)

    response = await client.post(
        "/v1/responses",
        headers={"openai-beta": "responses=v1"},
        json={
            **_RESPONSES_BODY,
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Lookup a value",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "previous_response_id": "resp_previous",
            "reasoning": {"effort": "high", "summary": "auto"},
        },
    )

    assert response.status_code == 200
    assert response.json()["id"] == "resp_native"
    assert response.headers["x-usage-prompt-tokens"] == "1"
    assert response.headers["x-usage-completion-tokens"] == "1"
    forwarded = native_responses.await_args_list[0].args[1]
    assert forwarded["previous_response_id"] == "resp_previous"
    assert forwarded["reasoning"] == {"effort": "high", "summary": "auto"}
    assert forwarded["tools"][0]["name"] == "lookup"
    assert native_responses.await_args_list[0].kwargs["upstream_headers"] == {
        "openai-beta": "responses=v1"
    }
    translated_chat.assert_not_awaited()


def test_responses_translation_forwards_stream_flag_without_raising():
    body = translate_responses_request({**_RESPONSES_BODY, "stream": True})
    assert body["stream"] is True


async def test_responses_streaming_returns_responses_sse(async_client):
    client, _ = async_client
    response = await client.post(
        "/v1/responses",
        json={**_RESPONSES_BODY, "stream": True},
    )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    content = response.content.decode()

    created_index = content.index('"type": "response.created"')
    delta_index = content.index('"type": "response.output_text.delta"')
    completed_index = content.index('"type": "response.completed"')
    assert created_index < delta_index < completed_index


async def test_responses_streaming_matches_non_streaming_text(async_client):
    client, _ = async_client

    streaming_response = await client.post(
        "/v1/responses",
        json={**_RESPONSES_BODY, "stream": True},
    )
    non_streaming_response = await client.post("/v1/responses", json=_RESPONSES_BODY)

    assert streaming_response.status_code == 200
    assert non_streaming_response.status_code == 200

    streamed_text = ""
    for line in streaming_response.content.decode().splitlines():
        if not line.startswith("data:"):
            continue
        payload = json.loads(line[len("data:"):].strip())
        if payload.get("type") == "response.output_text.delta":
            streamed_text += payload["delta"]

    assert streamed_text == non_streaming_response.json()["output_text"]
