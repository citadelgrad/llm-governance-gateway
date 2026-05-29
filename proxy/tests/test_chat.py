from __future__ import annotations

from proxy.app.governance_client import GovernanceError, InspectResponse
from proxy.app.main import app
from proxy.app.rate_limit import RateLimitResult

_CLEAN_BODY = {
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Hello, how are you?"}],
}

_STREAM_BODY = {
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": True,
}


async def test_clean_request(async_client):
    client, _ = async_client
    response = await client.post("/v1/chat/completions", json=_CLEAN_BODY)
    assert response.status_code == 200
    body = response.json()
    assert "choices" in body
    assert len(body["choices"]) > 0
    assert "message" in body["choices"][0]
    assert "content" in body["choices"][0]["message"]
    assert "usage" in body
    assert "total_tokens" in body["usage"]


async def test_rate_limit_denied(async_client):
    client, _ = async_client
    app.state.rate_limiter.check.return_value = RateLimitResult(
        allowed=False, retry_after_seconds=60, limit=100, remaining=0
    )
    response = await client.post("/v1/chat/completions", json=_CLEAN_BODY)
    assert response.status_code == 429
    assert "retry-after" in response.headers or "Retry-After" in response.headers
    retry_after = response.headers.get("Retry-After") or response.headers.get("retry-after")
    assert int(retry_after) > 0
    assert "x-ratelimit-limit-requests" in response.headers


async def test_governance_blocks(async_client):
    client, gov_mock = async_client
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
    body = response.json()
    assert body["error"]["type"] == "policy_violation"
    assert len(body["error"]["violations"]) > 0


async def test_pii_redaction_headers(async_client):
    client, gov_mock = async_client
    gov_mock.inspect.return_value = InspectResponse(
        decision="allow",
        redacted_text="My SSN is [REDACTED]",
        pii_findings=[{"type": "SSN", "start": 10, "end": 21}],
        harm_score=0.0,
        violations=[],
        audit_id="pii-audit-id",
    )
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "My SSN is 123-45-6789"}],
    }
    response = await client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    assert response.headers.get("x-gateway-pii-redacted") == "true"
    assert "SSN" in response.headers.get("x-gateway-pii-types", "")


async def test_phi_block_via_mock_provider(async_client):
    """Governance allows but mock provider blocks PHI content."""
    client, _ = async_client
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "Show me the patient diagnosis"}],
    }
    response = await client.post("/v1/chat/completions", json=body)
    assert response.status_code == 403
    resp_body = response.json()
    assert "error" in resp_body


async def test_unknown_model_returns_400(async_client):
    client, _ = async_client
    body = {
        "model": "totally-unknown-model-xyz",
        "messages": [{"role": "user", "content": "Hello"}],
    }
    response = await client.post("/v1/chat/completions", json=body)
    assert response.status_code == 400
    resp_body = response.json()
    assert resp_body["detail"]["error"]["type"] == "model_not_found"


async def test_streaming_response(async_client):
    client, _ = async_client
    response = await client.post("/v1/chat/completions", json=_STREAM_BODY)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    # At least one SSE data line should be present
    assert b"data:" in response.content


async def test_governance_unavailable_returns_503(async_client):
    client, gov_mock = async_client
    gov_mock.inspect.side_effect = GovernanceError("governance down")
    response = await client.post("/v1/chat/completions", json=_CLEAN_BODY)
    assert response.status_code == 503
    body = response.json()
    assert body["detail"]["error"]["type"] == "governance_unavailable"


async def test_rate_limit_headers_on_success(async_client):
    """Rate-limit headers are present on successful responses."""
    client, _ = async_client
    response = await client.post("/v1/chat/completions", json=_CLEAN_BODY)
    assert response.status_code == 200
    assert "x-ratelimit-limit-requests" in response.headers
    assert "x-ratelimit-remaining-requests" in response.headers
    assert "x-ratelimit-reset-requests" in response.headers
