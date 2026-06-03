"""
Docker Compose smoke tests.

Run with:  INTEGRATION_TEST=1 pytest tests/integration/
Requires:  make up  (proxy in mock mode: MOCK_PROVIDERS=true or OPENAI_API_KEY=mock)
"""

from __future__ import annotations

import os

import httpx
import pytest
from jose import jwt

SKIP = not os.environ.get("INTEGRATION_TEST")
GATEWAY_URL = os.environ.get("GATEWAY_BASE_URL", "http://localhost:8765")
JWT_SECRET = os.environ.get("JWT_SECRET", "")


def _jwt() -> str:
    return jwt.encode(
        {"user_id": "smoke-user", "tenant_id": "acme-corp", "roles": ["tier1"]},
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
