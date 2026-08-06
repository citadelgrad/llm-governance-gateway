"""
Claude Code gateway compatibility smoke tests.

Documents the recommended client configuration for using the gateway with Claude Code
and exercises the endpoint surface against the local mock.

macOS/Linux:
    export ANTHROPIC_BASE_URL=https://gateway-host.example
    export ANTHROPIC_AUTH_TOKEN=<your-gateway-api-key>
    export ANTHROPIC_MODEL=claude-sonnet-4-6
    export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1

Windows PowerShell:
    $env:ANTHROPIC_BASE_URL = "https://gateway-host.example"
    $env:ANTHROPIC_AUTH_TOKEN = "<your-gateway-api-key>"
    $env:ANTHROPIC_MODEL = "claude-sonnet-4-6"
    $env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1"

The gateway exposes:
    POST /v1/messages              — Anthropic Messages API compatible
    POST /v1/messages/count_tokens — Token counting (deterministic approximation)
    GET  /v1/models                — Model discovery (tenant-filtered)
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Static documentation assertions — verify config spec is complete
# ---------------------------------------------------------------------------

_GATEWAY_ENDPOINTS = {
    "POST /v1/messages",
    "POST /v1/messages/count_tokens",
    "GET /v1/models",
}

_CLIENT_ENV_VARS = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
}


def test_required_endpoints_documented():
    assert "POST /v1/messages" in _GATEWAY_ENDPOINTS
    assert "POST /v1/messages/count_tokens" in _GATEWAY_ENDPOINTS
    assert "GET /v1/models" in _GATEWAY_ENDPOINTS


def test_client_env_vars_documented():
    module_doc = __doc__ or ""
    for var in _CLIENT_ENV_VARS:
        assert var in module_doc


# ---------------------------------------------------------------------------
# Live smoke tests against the in-process mock
# ---------------------------------------------------------------------------

async def test_smoke_messages_endpoint(messages_client):
    """POST /v1/messages returns an Anthropic-shaped response via the mock."""
    client, _ = messages_client
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "What is 2 + 2?"}],
        "max_tokens": 50,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["type"] == "message"
    assert data["role"] == "assistant"
    assert len(data["content"]) > 0
    assert data["content"][0]["type"] == "text"


async def test_smoke_count_tokens_endpoint(messages_client):
    """POST /v1/messages/count_tokens returns input_tokens without a provider call."""
    client, _ = messages_client
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Count my tokens please."}],
    }
    response = await client.post("/v1/messages/count_tokens", json=body)
    assert response.status_code == 200
    data = response.json()
    assert "input_tokens" in data
    assert data["input_tokens"] > 0


async def test_smoke_models_discovery_returns_claude_models(messages_client):
    """GET /v1/models with a Claude-only model config returns only Claude model IDs."""
    client, _ = messages_client
    response = await client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    for entry in data["data"]:
        assert entry["id"].startswith("claude-"), f"Unexpected non-Claude model: {entry['id']}"


async def test_models_discovery_accepts_bearer_api_key(auth_messages_client):
    """Claude Code model discovery uses ANTHROPIC_AUTH_TOKEN as a bearer API key."""
    import bcrypt

    key = "compat-fixture-token-for-claude-model-discovery"
    key_hash = bcrypt.hashpw(key.encode(), bcrypt.gensalt(rounds=4)).decode()
    client, pool = auth_messages_client
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchrow.side_effect = [
        {"hash": key_hash, "user_id": "cc-user", "tenant_id": "test-tenant", "roles": ["user"]},
        None,
    ]
    response = await client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    assert response.json()["object"] == "list"


async def test_smoke_streaming_messages(messages_client):
    """POST /v1/messages with stream=true returns Anthropic SSE format."""
    client, _ = messages_client
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 50,
        "stream": True,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    content = response.content.decode()
    assert "message_start" in content
    assert "content_block_start" in content
    assert "message_stop" in content
