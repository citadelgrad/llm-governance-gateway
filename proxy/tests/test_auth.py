from __future__ import annotations

_HEALTH = "/health"


async def test_jwt_valid(auth_client, jwt_factory):
    """A correctly signed JWT is accepted."""
    client, _ = auth_client
    token = jwt_factory()
    response = await client.get(_HEALTH, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


async def test_jwt_tampered(auth_client, jwt_factory):
    """A JWT signed with the wrong secret is rejected with 401."""
    client, _ = auth_client
    bad_token = jwt_factory(secret="wrong-secret-key-that-is-not-the-real-one!!")
    response = await client.get("/v1/me", headers={"Authorization": f"Bearer {bad_token}"})
    assert response.status_code == 401


async def test_no_auth_header(auth_client):
    """Missing Authorization header returns 401 on a protected endpoint."""
    client, _ = auth_client
    response = await client.get("/v1/me")
    assert response.status_code == 401


async def test_api_key_valid(auth_client, api_key_creds):
    """A valid API key resolves to a caller and succeeds."""
    client, pool = auth_client
    raw_key, key_hash = api_key_creds
    conn = pool.acquire.return_value.__aenter__.return_value
    # First fetchrow = API key lookup, second = tenant info lookup (returns None → defaults)
    conn.fetchrow.side_effect = [
        {"hash": key_hash, "user_id": "key-user", "tenant_id": "test-tenant", "roles": ["user"]},
        None,
    ]
    response = await client.get(
        "/v1/me", headers={"Authorization": f"ApiKey {raw_key}"}
    )
    assert response.status_code == 200
    assert response.json()["user_id"] == "key-user"


async def test_openai_compatible_bearer_api_key_valid(auth_client, api_key_creds):
    """OpenAI-compatible clients send their configured API key as a bearer token."""
    client, pool = auth_client
    raw_key, key_hash = api_key_creds
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchrow.side_effect = [
        {"hash": key_hash, "user_id": "key-user", "tenant_id": "test-tenant", "roles": ["user"]},
        None,
    ]

    response = await client.get(
        "/v1/me", headers={"Authorization": f"Bearer {raw_key}"}
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == "key-user"


async def test_api_key_invalid(auth_client):
    """An API key with no matching DB row returns 401."""
    client, pool = auth_client
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.fetchrow.return_value = None
    response = await client.get(
        "/v1/me", headers={"Authorization": "ApiKey totally-invalid-key-xyz"}
    )
    assert response.status_code == 401


async def test_malformed_auth_scheme(auth_client):
    """An unrecognised auth scheme with a space in it returns 401."""
    client, _ = auth_client
    response = await client.get("/v1/me", headers={"Authorization": "Token some-value"})
    assert response.status_code == 401
