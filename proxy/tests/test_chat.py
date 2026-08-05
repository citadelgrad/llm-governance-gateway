from __future__ import annotations

from proxy.app.config import settings as app_settings
from proxy.app.governance_client import GovernanceError, InspectResponse
from proxy.app.main import app
from proxy.app.providers import mock as mock_provider
from proxy.app.rate_limit import RateLimitResult
from starlette.responses import Response as StarletteResponse

_CLEAN_BODY = {
    "model": "gpt-5.6-luna",
    "messages": [{"role": "user", "content": "Hello, how are you?"}],
}

_STREAM_BODY = {
    "model": "gpt-5.6-luna",
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


async def test_chat_rejects_unknown_top_level_semantics(async_client):
    client, _ = async_client

    response = await client.post(
        "/v1/chat/completions",
        json={**_CLEAN_BODY, "provider_magic": True},
    )

    assert response.status_code == 400
    violations = response.json()["detail"]["error"]["details"]["violations"]
    assert violations[0]["field"] == "provider_magic"
    assert violations[0]["type"] == "extra_forbidden"


async def test_chat_validates_conditional_stream_fields(async_client):
    client, _ = async_client

    response = await client.post(
        "/v1/chat/completions",
        json={**_CLEAN_BODY, "stream_options": {"include_usage": True}},
    )

    assert response.status_code == 400
    violation = response.json()["detail"]["error"]["details"]["violations"][0]
    assert violation["type"] == "value_error"
    assert "stream_options requires stream=true" in violation["message"]


async def test_chat_invalid_nested_image_url_returns_precise_field_path(async_client):
    client, gov_mock = async_client

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.6-luna",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {"type": "image_url", "image_url": {"url": 3}},
                    ],
                }
            ],
        },
    )

    assert response.status_code == 400
    violations = response.json()["detail"]["error"]["details"]["violations"]
    assert violations[0]["field"] == "messages.0.content.1.image_url.url"
    gov_mock.inspect.assert_not_awaited()


async def test_chat_accepts_current_moderation_object(async_client):
    client, _ = async_client

    response = await client.post(
        "/v1/chat/completions",
        json={
            **_CLEAN_BODY,
            "moderation": {
                "model": "omni-moderation-latest",
                "policy": {"input": {"mode": "block"}, "output": {"mode": "score"}},
            },
        },
    )

    assert response.status_code == 200


async def test_chat_rejects_unknown_modality_before_governance(async_client):
    client, gov_mock = async_client

    response = await client.post(
        "/v1/chat/completions",
        json={**_CLEAN_BODY, "modalities": ["telepathy"]},
    )

    assert response.status_code == 400
    violations = response.json()["detail"]["error"]["details"]["violations"]
    assert violations[0]["field"] == "modalities.0"
    gov_mock.inspect.assert_not_awaited()


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
    assert body["detail"]["error"]["type"] == "policy_violation"
    assert len(body["detail"]["error"]["violations"]) > 0


async def test_agent_client_missing_act_claim_rejected_on_chat_completions(auth_client, jwt_factory):
    """docs/auth-architecture.md: the act claim is required uniformly at every
    ingress leg (chat, github, mcp, cloud), not just MCP - a registered
    agent-runtime client ID with no act claim must be rejected here too,
    before governance is ever called."""
    client, _ = auth_client
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
        assert response.json()["detail"]["error"]["type"] == "missing_act_claim"
    finally:
        app_settings.agent_runtime_client_ids = original


async def test_agent_client_with_valid_act_claim_proceeds_on_chat_completions(
    auth_client, jwt_factory
):
    """The same agent-runtime client succeeds once it carries a valid,
    distinct act claim."""
    client, _ = auth_client
    original = app_settings.agent_runtime_client_ids
    app_settings.agent_runtime_client_ids = ["agent-runtime-1"]
    try:
        token = jwt_factory(client_id="agent-runtime-1", act_sub="delegating-human-user")

        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json=_CLEAN_BODY,
        )

        assert response.status_code == 200
    finally:
        app_settings.agent_runtime_client_ids = original


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
        "model": "gpt-5.6-luna",
        "messages": [{"role": "user", "content": "My SSN is 123-45-6789"}],
    }
    response = await client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    assert response.headers.get("x-gateway-pii-redacted") == "true"
    assert "SSN" in response.headers.get("x-gateway-pii-types", "")


async def test_technical_query_reaches_provider_unmodified(async_client, monkeypatch):
    """ai-gateway-qi5 end-to-end coverage: governance no longer flags ordinary
    technical/product proper nouns (e.g. "Django") as PERSON, so pii_findings
    is empty and main.py's redaction branch (`if inspect_resp.pii_findings: ...
    msg["content"] = inspect_resp.redacted_text`) never fires. Assert the
    mocked upstream provider receives the exact original text, not a
    [PERSON]-substituted version."""
    monkeypatch.setattr(app_settings, "mock_mode", True)
    client, gov_mock = async_client
    gov_mock.inspect.return_value = InspectResponse(
        decision="allow",
        redacted_text="",
        pii_findings=[],
        harm_score=0.0,
        violations=[],
        audit_id="django-audit-id",
    )

    received: dict = {}

    async def fake_chat_completions(body, extra_headers):
        received["body"] = body
        return StarletteResponse(
            content="{}", media_type="application/json", headers=extra_headers
        )

    monkeypatch.setattr(mock_provider, "chat_completions", fake_chat_completions)

    text = "what is the latest version of Django web framework"
    body = {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": text}]}
    response = await client.post("/v1/chat/completions", json=body)

    assert response.status_code == 200
    assert received["body"]["messages"][-1]["content"] == text
    assert "[PERSON]" not in received["body"]["messages"][-1]["content"]


async def test_phi_block_via_mock_provider(async_client, monkeypatch):
    """Governance allows but mock provider blocks PHI content."""
    from proxy.app.config import settings
    monkeypatch.setattr(settings, "mock_mode", True)
    client, _ = async_client
    body = {
        "model": "gpt-5.6-luna",
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
