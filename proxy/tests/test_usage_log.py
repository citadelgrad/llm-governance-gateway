from __future__ import annotations

from decimal import Decimal

from proxy.app.config import settings as app_settings
from proxy.app.governance_client import InspectResponse
from proxy.app.main import app
from proxy.app.rate_limit import RateLimitResult

_CLEAN_BODY = {
    "model": "gpt-5.6-luna",
    "messages": [{"role": "user", "content": "Hello, how are you?"}],
}


def _conn(pool):
    return pool.acquire.return_value.__aenter__.return_value


def _usage_log_insert(conn):
    """Return the positional args of the usage_log INSERT call, or None."""
    for call in conn.execute.call_args_list:
        if call.args and "INSERT INTO usage_log" in call.args[0]:
            return call.args
    return None


async def test_allowed_request_writes_real_tokens_and_computed_cost(async_client):
    """AC1: an allowed response writes real token counts and pricing-derived cost."""
    client, _ = async_client
    conn = _conn(app.state.db_pool)

    async def fetchrow(query, *args, **kwargs):
        if "FROM pricing" in query:
            return {
                "input_rate_usd_per_token": Decimal("0.000005"),
                "output_rate_usd_per_token": Decimal("0.000015"),
            }
        return None

    conn.fetchrow.side_effect = fetchrow

    response = await client.post("/v1/chat/completions", json=_CLEAN_BODY)

    assert response.status_code == 200
    call_args = _usage_log_insert(conn)
    assert call_args is not None
    (
        _query,
        _created_at,
        _tenant_id,
        _api_key_prefix,
        _user_id,
        model_id,
        status,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        cost_usd,
        latency_ms,
    ) = call_args
    assert status == "allowed"
    assert (prompt_tokens, completion_tokens, total_tokens) == (10, 5, 15)
    assert cost_usd == Decimal("0.000005") * 10 + Decimal("0.000015") * 5
    assert model_id == "gpt-5.6-luna"
    assert latency_ms >= 0


async def test_blocked_request_writes_zero_cost_row(async_client):
    """AC2: a governance block writes status='blocked' with 0 tokens and 0 cost."""
    client, gov_mock = async_client
    conn = _conn(app.state.db_pool)
    gov_mock.inspect.return_value = InspectResponse(
        decision="block",
        redacted_text="",
        pii_findings=[],
        harm_score=0.9,
        violations=["policy:data_classification_mismatch"],
        audit_id="block-audit-id",
    )

    response = await client.post("/v1/chat/completions", json=_CLEAN_BODY)

    assert response.status_code == 403
    call_args = _usage_log_insert(conn)
    assert call_args is not None
    status = call_args[6]
    prompt_tokens, completion_tokens, total_tokens = call_args[7:10]
    cost_usd = call_args[10]
    assert status == "blocked"
    assert (prompt_tokens, completion_tokens, total_tokens) == (0, 0, 0)
    assert cost_usd == Decimal("0")


async def test_errored_request_writes_zero_cost_row(async_client):
    """AC3: a pre-dispatch failure (rate limit) writes status='errored' with 0 tokens/cost."""
    client, _ = async_client
    conn = _conn(app.state.db_pool)
    app.state.rate_limiter.check.return_value = RateLimitResult(
        allowed=False, retry_after_seconds=60, limit=100, remaining=0
    )

    response = await client.post("/v1/chat/completions", json=_CLEAN_BODY)

    assert response.status_code == 429
    call_args = _usage_log_insert(conn)
    assert call_args is not None
    status = call_args[6]
    prompt_tokens, completion_tokens, total_tokens = call_args[7:10]
    cost_usd = call_args[10]
    assert status == "errored"
    assert (prompt_tokens, completion_tokens, total_tokens) == (0, 0, 0)
    assert cost_usd == Decimal("0")


async def test_auth_failure_produces_no_usage_log_row(auth_client, jwt_factory):
    """AC4: an in-pipeline 401 (missing act claim) writes no usage_log row."""
    client, pool = auth_client
    conn = _conn(pool)
    original = app_settings.agent_runtime_client_ids
    app_settings.agent_runtime_client_ids = ["agent-runtime-1"]
    try:
        token = jwt_factory(client_id="agent-runtime-1")
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json=_CLEAN_BODY,
        )
        assert response.status_code == 401
        assert _usage_log_insert(conn) is None
    finally:
        app_settings.agent_runtime_client_ids = original


async def test_model_id_resolves_alias_to_canonical(async_client):
    """AC5: usage_log.model_id is the canonical model, never the client-supplied alias."""
    client, _ = async_client
    conn = _conn(app.state.db_pool)
    app.state.models_by_id["gpt-5.6-luna"]["alias_of"] = "gpt-4o"
    try:
        response = await client.post("/v1/chat/completions", json=_CLEAN_BODY)
        assert response.status_code == 200
        call_args = _usage_log_insert(conn)
        assert call_args is not None
        assert call_args[5] == "gpt-4o"
    finally:
        del app.state.models_by_id["gpt-5.6-luna"]["alias_of"]


async def test_api_key_prefix_matches_authenticated_key(auth_client, api_key_creds):
    """AC6: usage_log.api_key_prefix matches the prefix of the authenticating API key."""
    client, pool = auth_client
    raw_key, key_hash = api_key_creds
    conn = _conn(pool)

    async def fetchrow(query, *args, **kwargs):
        if "FROM api_keys" in query:
            return {
                "hash": key_hash,
                "user_id": "key-user",
                "tenant_id": "test-tenant",
                "roles": ["user"],
            }
        return None

    conn.fetchrow.side_effect = fetchrow

    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"ApiKey {raw_key}"},
        json=_CLEAN_BODY,
    )

    assert response.status_code == 200
    call_args = _usage_log_insert(conn)
    assert call_args is not None
    assert call_args[3] == raw_key[:8]


async def test_missing_pricing_leaves_cost_null(async_client):
    """AC7: no effective pricing row -> cost_usd is null, tokens populated, request still succeeds."""
    client, _ = async_client
    conn = _conn(app.state.db_pool)
    # Default mock fetchrow returns None for both the tenant and pricing lookups.

    response = await client.post("/v1/chat/completions", json=_CLEAN_BODY)

    assert response.status_code == 200
    call_args = _usage_log_insert(conn)
    assert call_args is not None
    status = call_args[6]
    prompt_tokens, completion_tokens, total_tokens = call_args[7:10]
    cost_usd = call_args[10]
    assert status == "allowed"
    assert (prompt_tokens, completion_tokens, total_tokens) == (10, 5, 15)
    assert cost_usd is None


async def test_usage_log_write_failure_does_not_fail_request(async_client):
    """AC8: a DB failure while writing usage_log does not affect the client-facing response."""
    client, _ = async_client
    conn = _conn(app.state.db_pool)
    conn.execute.side_effect = RuntimeError("db unavailable")

    response = await client.post("/v1/chat/completions", json=_CLEAN_BODY)

    assert response.status_code == 200
    assert "choices" in response.json()


_STREAM_BODY_WITH_USAGE = {
    "model": "gpt-5.6-luna",
    "messages": [{"role": "user", "content": "Hello, how are you?"}],
    "stream": True,
    "stream_options": {"include_usage": True},
}

_STREAM_BODY_NO_USAGE = {
    "model": "gpt-5.6-luna",
    "messages": [{"role": "user", "content": "Hello, how are you?"}],
    "stream": True,
}


async def test_streaming_request_with_usage_event_writes_real_tokens(async_client):
    """Streaming AC1: a provider that emits usage in-stream produces a usage_log
    row with non-zero tokens matching the provider-reported values."""
    client, _ = async_client
    conn = _conn(app.state.db_pool)

    response = await client.post("/v1/chat/completions", json=_STREAM_BODY_WITH_USAGE)

    assert response.status_code == 200
    call_args = _usage_log_insert(conn)
    assert call_args is not None
    status = call_args[6]
    prompt_tokens, completion_tokens, total_tokens = call_args[7:10]
    assert status == "allowed"
    assert (prompt_tokens, completion_tokens, total_tokens) == (10, 5, 15)


async def test_streaming_request_without_usage_event_still_writes_zero_cost_row(async_client):
    """Streaming AC2: a provider that does not emit usage in-stream still writes a
    usage_log row with status='allowed' and 0 tokens/0 cost, rather than skipping it."""
    client, _ = async_client
    conn = _conn(app.state.db_pool)

    async def fetchrow(query, *args, **kwargs):
        if "FROM pricing" in query:
            return {
                "input_rate_usd_per_token": Decimal("0.000005"),
                "output_rate_usd_per_token": Decimal("0.000015"),
            }
        return None

    conn.fetchrow.side_effect = fetchrow

    response = await client.post("/v1/chat/completions", json=_STREAM_BODY_NO_USAGE)

    assert response.status_code == 200
    call_args = _usage_log_insert(conn)
    assert call_args is not None
    status = call_args[6]
    prompt_tokens, completion_tokens, total_tokens = call_args[7:10]
    cost_usd = call_args[10]
    assert status == "allowed"
    assert (prompt_tokens, completion_tokens, total_tokens) == (0, 0, 0)
    assert cost_usd == Decimal("0")


async def test_streaming_response_body_unchanged_by_usage_capture(async_client):
    """Streaming AC3: SSE format and translated chunks returned to the client are
    unchanged by the usage-capture wrapper."""
    client, _ = async_client

    response = await client.post("/v1/chat/completions", json=_STREAM_BODY_WITH_USAGE)

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert b"data:" in response.content
    assert response.content.rstrip().endswith(b"data: [DONE]")
