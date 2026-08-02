"""Tests for POST /v1/messages and POST /v1/messages/count_tokens."""
from __future__ import annotations

import bcrypt
from proxy.app.governance_client import GovernanceError, InspectResponse
from proxy.app.main import app
from proxy.app.rate_limit import RateLimitResult

# Pre-computed for compat-auth tests; low rounds so tests stay fast
_COMPAT_KEY = "compat-fixture-token-for-messages-tests"
_COMPAT_KEY_HASH = bcrypt.hashpw(_COMPAT_KEY.encode(), bcrypt.gensalt(rounds=4)).decode()

_BASE_BODY = {
    "model": "claude-3-5-sonnet",
    "messages": [{"role": "user", "content": "Hello, how are you?"}],
    "max_tokens": 100,
}


# ---------------------------------------------------------------------------
# Happy path — response shape and content
# ---------------------------------------------------------------------------

async def test_messages_happy_path(messages_client):
    client, _ = messages_client
    response = await client.post("/v1/messages", json=_BASE_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert isinstance(body["content"], list)
    assert body["content"][0]["type"] == "text"
    assert "usage" in body
    assert "input_tokens" in body["usage"]
    assert "output_tokens" in body["usage"]
    assert "stop_reason" in body


async def test_messages_response_is_anthropic_shaped_not_openai(messages_client):
    client, _ = messages_client
    response = await client.post("/v1/messages", json=_BASE_BODY)
    assert response.status_code == 200
    body = response.json()
    assert "choices" not in body
    assert body["type"] == "message"
    assert body["stop_reason"] in ("end_turn", "max_tokens", "tool_use", "stop_sequence")


async def test_messages_with_system_prompt(messages_client):
    client, _ = messages_client
    body = {
        "model": "claude-3-5-sonnet",
        "system": "You are a helpful assistant.",
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 50,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 200


async def test_messages_content_block_text_has_string(messages_client):
    client, _ = messages_client
    response = await client.post("/v1/messages", json=_BASE_BODY)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["content"][0].get("text"), str)


async def test_messages_streaming_returns_anthropic_sse(messages_client):
    client, _ = messages_client
    body = {**_BASE_BODY, "stream": True}
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    content = response.content.decode()
    assert "message_start" in content
    assert "content_block_start" in content


# ---------------------------------------------------------------------------
# Auth normalization tests
# ---------------------------------------------------------------------------

async def test_messages_auth_bearer_api_key(auth_messages_client):
    """Claude Code sends ANTHROPIC_AUTH_TOKEN as Authorization: Bearer ***."""
    client, pool = auth_messages_client
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchrow.side_effect = [
        {"hash": _COMPAT_KEY_HASH, "user_id": "cc-user", "tenant_id": "test-tenant", "roles": ["user"]},
        None,  # tenant lookup → use defaults (all models allowed)
    ]
    response = await client.post(
        "/v1/messages",
        json=_BASE_BODY,
        headers={"Authorization": f"Bearer {_COMPAT_KEY}"},
    )
    assert response.status_code == 200


async def test_messages_auth_x_api_key_header(auth_messages_client):
    """Anthropic SDK sends credentials as x-api-key header."""
    client, pool = auth_messages_client
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchrow.side_effect = [
        {"hash": _COMPAT_KEY_HASH, "user_id": "cc-user", "tenant_id": "test-tenant", "roles": ["user"]},
        None,
    ]
    response = await client.post(
        "/v1/messages",
        json=_BASE_BODY,
        headers={"x-api-key": _COMPAT_KEY},
    )
    assert response.status_code == 200


async def test_messages_auth_missing_returns_401(auth_messages_client):
    client, _ = auth_messages_client
    response = await client.post("/v1/messages", json=_BASE_BODY)
    assert response.status_code == 401


async def test_messages_auth_invalid_key_returns_401(auth_messages_client):
    client, pool = auth_messages_client
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchrow.return_value = None  # key not found
    response = await client.post(
        "/v1/messages",
        json=_BASE_BODY,
        headers={"x-api-key": "bad-key-that-does-not-exist"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Model denial / routing
# ---------------------------------------------------------------------------

async def test_messages_unknown_model_returns_400(messages_client):
    client, _ = messages_client
    body = {
        "model": "totally-unknown-model-xyz",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 50,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "model_not_found"


async def test_messages_accepts_tool_definitions(messages_client):
    client, _ = messages_client
    body = {
        **_BASE_BODY,
        "tools": [
            {
                "name": "lookup",
                "description": "Look something up",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
        ],
        "tool_choice": {"type": "auto"},
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 200


async def test_messages_accepts_tool_result_content_blocks(messages_client):
    client, _ = messages_client
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "user", "content": "What's the weather?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "NYC"}}
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "Sunny, 72F"}],
            },
        ],
        "max_tokens": 50,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 200


async def test_messages_rejects_genuinely_unsupported_content_blocks(messages_client):
    client, _ = messages_client
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}],
            }
        ],
        "max_tokens": 50,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "unsupported_message_shape"


# ---------------------------------------------------------------------------
# Governance / PII tests — same controls as /v1/chat/completions
# ---------------------------------------------------------------------------

async def test_messages_governance_block(messages_client):
    client, gov_mock = messages_client
    gov_mock.inspect.return_value = InspectResponse(
        decision="block",
        redacted_text="",
        pii_findings=[],
        harm_score=0.9,
        violations=["policy:data_classification_mismatch"],
        audit_id="block-audit-id",
    )
    response = await client.post("/v1/messages", json=_BASE_BODY)
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "policy_violation"


async def test_messages_pii_headers_present(messages_client):
    client, gov_mock = messages_client
    gov_mock.inspect.return_value = InspectResponse(
        decision="allow",
        redacted_text="My SSN is [REDACTED]",
        pii_findings=[{"type": "SSN", "start": 10, "end": 21}],
        harm_score=0.0,
        violations=[],
        audit_id="pii-audit-id",
    )
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "My SSN is 123-45-6789"}],
        "max_tokens": 50,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 200
    assert response.headers.get("x-gateway-pii-redacted") == "true"
    assert "SSN" in response.headers.get("x-gateway-pii-types", "")


async def test_messages_governance_unavailable_503(messages_client):
    client, gov_mock = messages_client
    gov_mock.inspect.side_effect = GovernanceError("governance down")
    response = await client.post("/v1/messages", json=_BASE_BODY)
    assert response.status_code == 503
    assert response.json()["detail"]["error"]["type"] == "governance_unavailable"


async def test_messages_rate_limit_headers_present_on_success(messages_client):
    client, _ = messages_client
    response = await client.post("/v1/messages", json=_BASE_BODY)
    assert response.status_code == 200
    assert "x-ratelimit-limit-requests" in response.headers
    assert "x-ratelimit-remaining-requests" in response.headers
    assert "x-ratelimit-reset-requests" in response.headers


async def test_messages_rate_limit_denied_returns_429(messages_client):
    client, _ = messages_client
    app.state.rate_limiter.check.return_value = RateLimitResult(
        allowed=False, retry_after_seconds=60, limit=100, remaining=0
    )
    response = await client.post("/v1/messages", json=_BASE_BODY)
    assert response.status_code == 429


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------

async def test_count_tokens_basic(messages_client):
    client, _ = messages_client
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "Hello, how are you today?"}],
    }
    response = await client.post("/v1/messages/count_tokens", json=body)
    assert response.status_code == 200
    resp = response.json()
    assert "input_tokens" in resp
    assert isinstance(resp["input_tokens"], int)
    assert resp["input_tokens"] > 0


async def test_count_tokens_more_with_system_prompt(messages_client):
    client, _ = messages_client
    with_system = {
        "model": "claude-3-5-sonnet",
        "system": "You are a helpful assistant. " * 10,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    without_system = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    r1 = await client.post("/v1/messages/count_tokens", json=with_system)
    r2 = await client.post("/v1/messages/count_tokens", json=without_system)
    assert r1.json()["input_tokens"] > r2.json()["input_tokens"]


async def test_count_tokens_deterministic(messages_client):
    client, _ = messages_client
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "What is 2+2?"}],
    }
    r1 = await client.post("/v1/messages/count_tokens", json=body)
    r2 = await client.post("/v1/messages/count_tokens", json=body)
    assert r1.json()["input_tokens"] == r2.json()["input_tokens"]


async def test_count_tokens_unknown_model_returns_400(messages_client):
    client, _ = messages_client
    body = {
        "model": "totally-unknown-xyz",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    response = await client.post("/v1/messages/count_tokens", json=body)
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "model_not_found"


async def test_count_tokens_disallowed_model_returns_403(auth_messages_client):
    client, pool = auth_messages_client
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchrow.side_effect = [
        {"hash": _COMPAT_KEY_HASH, "user_id": "cc-user", "tenant_id": "test-tenant", "roles": ["user"]},
        {
            "default_provider": "anthropic",
            "allowed_models": ["claude-3-5-sonnet"],
            "pii_redaction_notification": "header",
            "rate_limit_requests_per_minute": 100,
        },
    ]
    body = {
        "model": "claude-haiku-4-5-20251001",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    response = await client.post(
        "/v1/messages/count_tokens",
        json=body,
        headers={"x-api-key": _COMPAT_KEY},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "model_not_allowed"


async def test_count_tokens_auth_missing_returns_401(auth_messages_client):
    client, _ = auth_messages_client
    body = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    response = await client.post("/v1/messages/count_tokens", json=body)
    assert response.status_code == 401
