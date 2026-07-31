from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from mcpproxy.app.main import app


def _setup_app_state(downstream_client):
    """Set app.state directly - ASGITransport does not fire the ASGI lifespan."""
    app.state.downstream_client = downstream_client


@pytest.fixture
async def async_client():
    """ASGI client with a mocked downstream client on app.state."""
    downstream_client = AsyncMock()
    _setup_app_state(downstream_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, downstream_client
