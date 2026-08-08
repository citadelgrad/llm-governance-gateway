from __future__ import annotations

from unittest.mock import AsyncMock

from proxy.app.auth import CallerContext
from proxy.app.config import settings as app_settings
from proxy.app.main import app, get_caller
from proxy.app.providers import gemini as gemini_provider
from proxy.app.providers import gemini_vertex as gemini_vertex_provider
from starlette.responses import Response as StarletteResponse

_MODELS_CONFIG = [
    {"id": "gemini-3.1-flash-lite", "provider": "gemini"},
    {"id": "gemini-3.1-flash-lite-vertex", "provider": "gemini-vertex"},
]


def _use_gemini_models(app_):
    app_.state.models_config = _MODELS_CONFIG
    app_.state.models_by_id = {m["id"]: m for m in _MODELS_CONFIG}


async def test_gemini_vertex_dispatch_invokes_vertex_client_not_gemini_client(
    async_client, monkeypatch
):
    """AC3: a request resolving to provider "gemini-vertex" invokes the
    Vertex client's chat_completions, not the API-key gemini client."""
    client, _ = async_client
    _use_gemini_models(app)
    monkeypatch.setattr(app_settings, "mock_mode", False)

    vertex_client_sentinel = object()
    app.state.gemini_vertex_client = vertex_client_sentinel
    app.state.gemini_client = object()

    vertex_mock = AsyncMock(
        return_value=StarletteResponse(content=b"{}", media_type="application/json")
    )
    gemini_mock = AsyncMock(
        side_effect=AssertionError("gemini client must not be used for gemini-vertex dispatch")
    )
    monkeypatch.setattr(gemini_vertex_provider, "chat_completions", vertex_mock)
    monkeypatch.setattr(gemini_provider, "chat_completions", gemini_mock)

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.1-flash-lite-vertex",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 200
    vertex_mock.assert_awaited_once()
    assert vertex_mock.await_args.args[0] is vertex_client_sentinel
    gemini_mock.assert_not_awaited()


async def test_gemini_dispatch_unaffected_by_gemini_vertex_addition(async_client, monkeypatch):
    """AC4: a request resolving to provider "gemini" still invokes the
    existing API-key gemini client, exactly as before this change."""
    client, _ = async_client
    _use_gemini_models(app)
    monkeypatch.setattr(app_settings, "mock_mode", False)

    gemini_client_sentinel = object()
    app.state.gemini_client = gemini_client_sentinel
    app.state.gemini_vertex_client = object()

    gemini_mock = AsyncMock(
        return_value=StarletteResponse(content=b"{}", media_type="application/json")
    )
    vertex_mock = AsyncMock(
        side_effect=AssertionError("vertex client must not be used for gemini dispatch")
    )
    monkeypatch.setattr(gemini_provider, "chat_completions", gemini_mock)
    monkeypatch.setattr(gemini_vertex_provider, "chat_completions", vertex_mock)

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.1-flash-lite",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 200
    gemini_mock.assert_awaited_once()
    assert gemini_mock.await_args.args[0] is gemini_client_sentinel
    vertex_mock.assert_not_awaited()


async def test_gemini_vertex_dispatch_without_configured_client_returns_clean_error(
    async_client, monkeypatch
):
    """AC5: dispatching to gemini-vertex when it was never configured at
    startup (app.state.gemini_vertex_client is None) returns a clear 5xx
    config error rather than an unhandled AttributeError."""
    client, _ = async_client
    _use_gemini_models(app)
    monkeypatch.setattr(app_settings, "mock_mode", False)

    app.state.gemini_vertex_client = None

    vertex_mock = AsyncMock(
        side_effect=AssertionError("adapter must not be invoked when client is unconfigured")
    )
    monkeypatch.setattr(gemini_vertex_provider, "chat_completions", vertex_mock)

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.1-flash-lite-vertex",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert response.status_code == 500
    assert response.json()["detail"]["error"]["type"] == "provider_not_configured"
    vertex_mock.assert_not_awaited()


async def test_header_override_with_permission_routes_gemini_catalog_model_to_vertex(
    async_client, monkeypatch
):
    """Epic scenario: an authorized caller's x-gateway-provider header
    overrides a catalog entry of provider "gemini", dispatching to Vertex."""
    client, _ = async_client
    _use_gemini_models(app)
    monkeypatch.setattr(app_settings, "mock_mode", False)
    app.dependency_overrides[get_caller] = lambda: CallerContext(
        user_id="override-user",
        tenant_id="test-tenant",
        roles=["gateway:provider_override:gemini-vertex"],
    )

    app.state.gemini_vertex_client = object()
    app.state.gemini_client = object()

    vertex_mock = AsyncMock(
        return_value=StarletteResponse(content=b"{}", media_type="application/json")
    )
    gemini_mock = AsyncMock(
        side_effect=AssertionError("gemini client must not be used when override selects vertex")
    )
    monkeypatch.setattr(gemini_vertex_provider, "chat_completions", vertex_mock)
    monkeypatch.setattr(gemini_provider, "chat_completions", gemini_mock)

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.1-flash-lite",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"x-gateway-provider": "gemini-vertex"},
    )

    assert response.status_code == 200
    vertex_mock.assert_awaited_once()
    gemini_mock.assert_not_awaited()


async def test_header_override_without_permission_is_denied_not_dispatched(
    async_client, monkeypatch
):
    """A caller without the gemini-vertex override permission cannot force
    Vertex dispatch via header; the request is denied, not silently routed
    to the catalog/tenant default provider."""
    client, _ = async_client
    _use_gemini_models(app)
    monkeypatch.setattr(app_settings, "mock_mode", False)
    app.dependency_overrides[get_caller] = lambda: CallerContext(
        user_id="plain-user", tenant_id="test-tenant", roles=["user"]
    )

    app.state.gemini_vertex_client = object()
    app.state.gemini_client = object()

    vertex_mock = AsyncMock(
        side_effect=AssertionError("vertex client must not be used without override permission")
    )
    gemini_mock = AsyncMock(
        side_effect=AssertionError("gemini client must not be used for a denied override request")
    )
    monkeypatch.setattr(gemini_vertex_provider, "chat_completions", vertex_mock)
    monkeypatch.setattr(gemini_provider, "chat_completions", gemini_mock)

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.1-flash-lite",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"x-gateway-provider": "gemini-vertex"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"]["type"] == "override_denied"
    vertex_mock.assert_not_awaited()
    gemini_mock.assert_not_awaited()
