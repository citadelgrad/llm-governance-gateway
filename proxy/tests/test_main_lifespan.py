from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from proxy.app import main as main_module
from proxy.app.config import settings as app_settings
from proxy.app.providers import gemini_vertex as gemini_vertex_provider


class _FakeVertexClient:
    def __init__(self) -> None:
        self.aclose = AsyncMock()


@pytest.fixture
def _lifespan_mocks(monkeypatch):
    """Stub every external resource lifespan() touches besides the
    gemini-vertex client construction under test, so entering/exiting the
    real lifespan() context manager doesn't require a live Postgres/Redis."""
    pool = MagicMock()
    pool.close = AsyncMock()
    monkeypatch.setattr(main_module.asyncpg, "create_pool", AsyncMock(return_value=pool))

    redis = MagicMock()
    redis.script_load = AsyncMock(return_value="fake-sha")
    redis.aclose = AsyncMock()
    monkeypatch.setattr(main_module.Redis, "from_url", MagicMock(return_value=redis))

    monkeypatch.setattr(main_module, "load_models_yaml", MagicMock(return_value=[]))
    monkeypatch.setattr(main_module, "maybe_bootstrap", AsyncMock())
    return pool, redis


async def test_gemini_vertex_client_constructed_when_project_id_set(monkeypatch, _lifespan_mocks):
    """AC1: gemini_vertex.make_client(settings) is called during the
    lifespan/startup block when gemini_vertex_project_id is set."""
    monkeypatch.setattr(app_settings, "gemini_vertex_project_id", "proj-x")
    fake_client = _FakeVertexClient()
    spy = MagicMock(return_value=fake_client)
    monkeypatch.setattr(gemini_vertex_provider, "make_client", spy)

    app = FastAPI()
    async with main_module.lifespan(app):
        spy.assert_called_once_with(app_settings)
        assert app.state.gemini_vertex_client is fake_client

    fake_client.aclose.assert_awaited_once()


async def test_gemini_vertex_client_not_constructed_when_project_id_unset(
    monkeypatch, _lifespan_mocks
):
    """AC2: gemini_vertex.make_client is not called (no credential loading
    attempted) when gemini_vertex_project_id is empty."""
    monkeypatch.setattr(app_settings, "gemini_vertex_project_id", "")
    spy = MagicMock()
    monkeypatch.setattr(gemini_vertex_provider, "make_client", spy)

    app = FastAPI()
    async with main_module.lifespan(app):
        spy.assert_not_called()
        assert app.state.gemini_vertex_client is None
