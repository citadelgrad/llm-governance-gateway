from __future__ import annotations

from proxy.app.main import app


async def test_health_ok(async_client):
    client, _ = async_client
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_not_ready(async_client):
    client, _ = async_client
    app.state.ready = False
    try:
        response = await client.get("/health")
        assert response.status_code == 503
    finally:
        app.state.ready = True
