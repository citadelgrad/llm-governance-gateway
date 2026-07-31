from __future__ import annotations

from proxy.app.governance_client import InspectResponse
from proxy.app.main import app
from proxy.app.responses_compat import translate_responses_request

_RESPONSES_BODY = {
    "model": "gpt-5.6-luna",
    "input": "Reply with gateway-ok only.",
}


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
