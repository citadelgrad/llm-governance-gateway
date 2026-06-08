"""
Docker Compose smoke tests.

Run with:  INTEGRATION_TEST=1 pytest tests/integration/
Requires:  make up  (proxy in mock mode: MOCK_PROVIDERS=true or OPENAI_API_KEY=mock)
"""

from __future__ import annotations

import os
import time

import httpx
import pytest
from jose import jwt

SKIP = not os.environ.get("INTEGRATION_TEST")
GATEWAY_URL = os.environ.get("GATEWAY_BASE_URL", "http://localhost:8765")
JWT_SECRET = os.environ.get("JWT_SECRET", "")


def _jwt(user_id: str = "smoke-user") -> str:
    return jwt.encode(
        {
            "user_id": user_id,
            "tenant_id": "acme-corp",
            "roles": ["tier1"],
            "iat": int(time.time()),
            "nbf": int(time.time()),
            "exp": int(time.time()) + 900,
        },
        JWT_SECRET,
        algorithm="HS256",
    )


@pytest.mark.skipif(SKIP, reason="Set INTEGRATION_TEST=1 to run stack smoke tests")
@pytest.mark.asyncio
class TestSmoke:
    async def test_health(self):
        async with httpx.AsyncClient(base_url=GATEWAY_URL) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_unauthed_is_401(self):
        async with httpx.AsyncClient(base_url=GATEWAY_URL) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        assert response.status_code == 401

    async def test_models_list(self):
        async with httpx.AsyncClient(base_url=GATEWAY_URL) as client:
            response = await client.get(
                "/v1/models", headers={"Authorization": f"Bearer {_jwt()}"}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "list"
        assert isinstance(body["data"], list)

    async def test_chat_roundtrip(self):
        async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {_jwt()}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "Hello, world!"}],
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert "choices" in body
        assert len(body["choices"]) > 0
        assert body["choices"][0]["message"]["content"]

    async def test_responses_roundtrip(self):
        async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
            response = await client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {_jwt()}"},
                json={
                    "model": "gpt-4o-mini",
                    "input": "Reply with gateway-ok only.",
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "response"
        assert body["output"]
        assert body["output_text"]

    async def test_pii_redaction_headers(self):
        async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30.0) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {_jwt(user_id='smoke-pii-user')}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "user", "content": "My SSN is 123-45-6789, can you help?"}
                    ],
                },
            )
        assert response.status_code == 200
        assert response.headers.get("x-gateway-pii-redacted") == "true"
        assert response.headers.get("x-gateway-pii-types") == "US_SSN"
        assert response.headers.get("x-audit-id")
