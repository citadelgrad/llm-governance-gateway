from __future__ import annotations


async def test_list_models(async_client):
    client, _ = async_client
    response = await client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    for entry in body["data"]:
        assert "id" in entry
        assert "object" in entry
        assert entry["object"] == "model"


async def test_list_models_returns_codex_catalog_envelope_for_codex_client(async_client):
    client, _ = async_client

    response = await client.get("/v1/models?client_version=0.145.0")

    assert response.status_code == 200
    assert response.json() == {"models": []}


async def test_me(async_client):
    client, _ = async_client
    response = await client.get("/v1/me")
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "test-user"
    assert body["tenant_id"] == "test-tenant"
    assert "roles" in body
    assert "allowed_models" in body
    assert "rate_limit" in body
    assert "requests_per_minute" in body["rate_limit"]
    assert "resets_at" in body["rate_limit"]
    assert "pii_policy" in body
    assert "notification" in body["pii_policy"]
