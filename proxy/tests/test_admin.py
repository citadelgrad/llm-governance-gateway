from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from proxy.app.main import app


def _gov_http_response(status: int = 200, body: bytes = b'{"data":[]}'):
    resp = MagicMock()
    resp.status_code = status
    resp.content = body
    resp.headers = {"content-type": "application/json"}
    return resp


# ---------------------------------------------------------------------------
# POST /v1/keys
# ---------------------------------------------------------------------------


async def test_create_key_admin(admin_client):
    client, _ = admin_client
    response = await client.post(
        "/v1/keys",
        json={"user_id": "new-user", "tenant_id": "test-tenant", "roles": ["user"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert "key" in body
    assert isinstance(body["key"], str)
    assert len(body["key"]) > 0


async def test_create_key_non_admin(async_client):
    client, _ = async_client
    response = await client.post(
        "/v1/keys",
        json={"user_id": "new-user", "tenant_id": "test-tenant"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "forbidden"


# ---------------------------------------------------------------------------
# GET /v1/audit
# ---------------------------------------------------------------------------


async def test_audit_admin(admin_client):
    client, _ = admin_client
    app.state.gov_http.get = AsyncMock(return_value=_gov_http_response(200, b'{"data":[]}'))
    response = await client.get("/v1/audit")
    assert response.status_code == 200


async def test_audit_non_admin(async_client):
    client, _ = async_client
    response = await client.get("/v1/audit")
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "forbidden"


# ---------------------------------------------------------------------------
# DELETE /v1/users/{user_id}
# ---------------------------------------------------------------------------


async def test_delete_user_admin(admin_client):
    client, _ = admin_client
    mock_resp = _gov_http_response(202, b'{"status":"accepted"}')
    mock_resp.status_code = 202

    # The endpoint wraps gov_http.delete() in `async with gov_http` which
    # closes the client — wire up both the context manager and the delete call.
    app.state.gov_http.__aenter__ = AsyncMock(return_value=app.state.gov_http)
    app.state.gov_http.__aexit__ = AsyncMock(return_value=False)
    app.state.gov_http.delete = AsyncMock(return_value=mock_resp)

    response = await client.delete("/v1/users/some-user-id")
    assert response.status_code == 202


async def test_delete_user_non_admin(async_client):
    client, _ = async_client
    response = await client.delete("/v1/users/some-user-id")
    assert response.status_code == 403
    assert response.json()["detail"]["error"]["type"] == "forbidden"
