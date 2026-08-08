"""Real-client protocol smoke fixtures (ai-gateway-7v9).

Compose/live smoke coverage before this file used simplified payloads, so the
request shapes real agent clients actually send — Codex CLI's bearer API-key
model discovery, an OpenAI-compatible client's streaming tool-augmented chat
completions, Codex's Responses streaming+tools traffic, and Claude Code's
two-turn tool loop over /v1/messages — went untested even though basic checks
passed. These
tests run entirely against the provider-free mock stack (ASGITransport, no
Docker/network) and parse structured JSON/event data rather than doing
substring matching on raw response bodies.
"""

from __future__ import annotations

import json

from proxy.app.providers import mock as mock_provider
from starlette.responses import Response as StarletteResponse

# ---------------------------------------------------------------------------
# 1. Bearer API-key model discovery (Codex CLI / OpenAI-compatible clients)
# ---------------------------------------------------------------------------


async def test_models_bearer_api_key_returns_realistic_list(auth_client, api_key_creds):
    """OpenAI-compatible agent CLIs (Codex CLI and other OpenAI-compatible
    clients) configure their gateway credential as a bare API key sent as
    `Authorization: Bearer <key>`, not a JWT. /v1/models uses the same
    get_caller_compat auth family as /v1/messages, but the existing
    test_models.py coverage only exercises it through the caller-override
    fixture (auth bypassed) — real bearer-API-key auth against this specific
    endpoint was untested.
    """
    client, pool = auth_client
    raw_key, key_hash = api_key_creds
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchrow.side_effect = [
        {
            "hash": key_hash,
            "user_id": "codex-cli-user",
            "tenant_id": "test-tenant",
            "roles": ["user"],
        },
        None,  # tenant lookup miss -> defaults to "all models allowed"
    ]

    response = await client.get("/v1/models", headers={"Authorization": f"Bearer {raw_key}"})

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list) and body["data"]
    for entry in body["data"]:
        assert entry["object"] == "model"
        assert isinstance(entry["id"], str) and entry["id"]
        assert isinstance(entry["owned_by"], str) and entry["owned_by"]
        assert isinstance(entry["created"], int)
    assert {entry["id"] for entry in body["data"]} == {"gpt-5.6-luna", "gpt-4o"}


# ---------------------------------------------------------------------------
# 2. OpenAI-compatible streaming tool-augmented chat-completions request
# ---------------------------------------------------------------------------

_STREAMING_TOOL_AUGMENTED_BODY = {
    "model": "gpt-5.6-luna",
    "messages": [
        {"role": "system", "content": "You are in agent mode."},
        {"role": "user", "content": "Name Oregon's capital city."},
    ],
    "stream": True,
    "stream_options": {"include_usage": True},
    "max_tokens": 128,
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file only when needed.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ],
}


async def test_streaming_tool_augmented_chat_is_structurally_valid(async_client):
    """OpenAI-compatible IDE agent clients send chat-completions streaming
    requests shaped like scripts/live_smoke.py's streaming tool-augmented
    chat check: a `tools` definition alongside `stream_options.include_usage`,
    terminated by `data: [DONE]`. Parse every SSE frame as JSON (not a
    substring match) and confirm the gateway accepts this request shape and
    produces a well-formed chunk stream.
    """
    client, _ = async_client
    response = await client.post("/v1/chat/completions", json=_STREAMING_TOOL_AUGMENTED_BODY)

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    data_lines = [line for line in response.text.splitlines() if line.startswith("data:")]
    assert data_lines, "no SSE data lines in response"
    assert data_lines[-1] == "data: [DONE]"

    events = [json.loads(line[len("data:") :].strip()) for line in data_lines[:-1]]
    assert events, "no JSON chunk events before [DONE]"
    for event in events:
        assert event["object"] == "chat.completion.chunk"
        assert event["model"] == "gpt-5.6-luna"
        assert isinstance(event["choices"], list) and event["choices"]

    assistant_text = "".join(event["choices"][0]["delta"].get("content", "") for event in events)
    assert assistant_text.strip(), "stream completed without any assistant text"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"

    # ai-gateway-wirs.6 taught mock.py's _stream_sse() to honor
    # stream_options.include_usage, matching real upstream OpenAI-compatible
    # providers: exactly the final chunk (before [DONE]) carries usage.
    usage_events = [event for event in events if "usage" in event]
    assert usage_events == [events[-1]]
    assert usage_events[0]["usage"]["total_tokens"] > 0


# ---------------------------------------------------------------------------
# 3. Codex Responses streaming + tools -> documented gap (ai-gateway-bih)
# ---------------------------------------------------------------------------


async def test_responses_streaming_with_tools_is_a_known_gap(async_client):
    """Codex CLI's Responses-API traffic pairs `stream: true` with a `tools`
    definition. ai-gateway-bih only implemented /v1/responses streaming for
    text-only requests and explicitly did not add tool/function support.
    ai-gateway-7v9 re-confirmed this is a real, current gap:
    translate_responses_request() (proxy/app/responses_compat.py) raises
    ResponsesCompatError("Responses tools are not supported yet") as soon as
    `req.tools` is truthy, before streaming is ever considered, and main.py
    converts that into a 422 `unsupported_response_shape` error.

    This test pins that ACTUAL current behavior. If a future change adds
    Responses tool support, this test will fail as a deliberate reminder to
    update or remove it, rather than letting the behavior change silently.
    It is a known, intentional limitation, not a bug to be fixed here.
    """
    client, _ = async_client
    response = await client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.6-luna",
            "input": "Look up today's date using the available tool.",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "get_date",
                    "description": "Return today's date.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
    )

    assert response.status_code == 422
    error = response.json()["detail"]["error"]
    assert error["type"] == "unsupported_response_shape"
    assert "tools" in error["message"].lower()


# ---------------------------------------------------------------------------
# 4. Two-turn Claude tool loop via /v1/messages (ai-gateway-mjt)
# ---------------------------------------------------------------------------

_WEATHER_TOOL = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


async def test_claude_two_turn_tool_loop_via_messages(messages_client, monkeypatch):
    """A real two-turn Claude Code tool loop through /v1/messages. The mock
    provider can never itself emit tool_calls (proxy/app/providers/mock.py's
    scenarios are pure text responses), so mock_provider.chat_completions is
    monkeypatched per-turn — the same technique test_chat.py's
    test_technical_query_reaches_provider_unmodified already uses — to
    exercise the real (unmocked) messages_to_chat_body / chat_response_to_
    anthropic translation logic in both directions.

    Turn 1 sends `tools`; the mocked model responds with a `tool_use` block.
    Turn 2 replays the conversation with that tool_use block plus a
    `tool_result` block appended, and the mocked model returns a final text
    answer. This exercises ai-gateway-mjt's tool-use support end-to-end and is
    expected to fully pass.
    """
    client, _ = messages_client

    # --- Turn 1: model calls the tool ---
    turn1_received: dict = {}

    async def fake_turn1(body, extra_headers):
        turn1_received["body"] = body
        return StarletteResponse(
            content=json.dumps(
                {
                    "id": "chatcmpl-mock-tool",
                    "object": "chat.completion",
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_abc123",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": json.dumps({"city": "NYC"}),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
                }
            ),
            media_type="application/json",
            headers=extra_headers,
        )

    monkeypatch.setattr(mock_provider, "chat_completions", fake_turn1)

    turn1_body = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "What's the weather in NYC?"}],
        "max_tokens": 200,
        "tools": _WEATHER_TOOL,
    }
    turn1_response = await client.post("/v1/messages", json=turn1_body)

    assert turn1_response.status_code == 200
    turn1_result = turn1_response.json()
    assert turn1_result["stop_reason"] == "tool_use"
    assert turn1_result["content"] == [
        {"type": "tool_use", "id": "call_abc123", "name": "get_weather", "input": {"city": "NYC"}}
    ]
    # The tool definition itself must have survived Anthropic->OpenAI translation.
    assert turn1_received["body"]["tools"][0]["function"]["name"] == "get_weather"

    # --- Turn 2: replay the conversation with the tool result ---
    turn2_received: dict = {}

    async def fake_turn2(body, extra_headers):
        turn2_received["body"] = body
        return StarletteResponse(
            content=json.dumps(
                {
                    "id": "chatcmpl-mock-final",
                    "object": "chat.completion",
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "It's 72F and sunny in NYC.",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
                }
            ),
            media_type="application/json",
            headers=extra_headers,
        )

    monkeypatch.setattr(mock_provider, "chat_completions", fake_turn2)

    turn2_body = {
        "model": "claude-sonnet-4-6",
        "messages": [
            {"role": "user", "content": "What's the weather in NYC?"},
            {"role": "assistant", "content": turn1_result["content"]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_abc123", "content": "72F and sunny"}
                ],
            },
        ],
        "max_tokens": 200,
        "tools": _WEATHER_TOOL,
    }
    turn2_response = await client.post("/v1/messages", json=turn2_body)

    assert turn2_response.status_code == 200
    turn2_result = turn2_response.json()
    assert turn2_result["stop_reason"] == "end_turn"
    assert turn2_result["content"] == [{"type": "text", "text": "It's 72F and sunny in NYC."}]

    # The tool_result block must have been translated into an OpenAI
    # role:"tool" message carrying the same id as turn 1's tool_use block.
    sent_messages = turn2_received["body"]["messages"]
    tool_messages = [m for m in sent_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_abc123"
    assert tool_messages[0]["content"] == "72F and sunny"
    # And turn 1's tool_use block must have survived the reverse translation
    # back into an OpenAI assistant message with tool_calls.
    assistant_messages = [m for m in sent_messages if m.get("role") == "assistant"]
    assert assistant_messages[-1]["tool_calls"][0]["id"] == "call_abc123"
