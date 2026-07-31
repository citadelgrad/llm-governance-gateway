from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from mcpproxy.app.main import app


def _setup_app_state(downstream_client, governance_client):
    """Set app.state directly - ASGITransport does not fire the ASGI lifespan."""
    app.state.downstream_client = downstream_client
    app.state.governance_client = governance_client


@pytest.fixture
async def async_client():
    """ASGI client with mocked downstream and governance clients on app.state.

    governance_client.scan_for_pii defaults to a successful no-op scan so
    existing forward/block assertions around buffering don't also need to
    stub the DLP checkpoint; tests exercising the checkpoint itself override
    scan_for_pii's side effect.
    """
    downstream_client = AsyncMock()
    governance_client = AsyncMock()
    governance_client.scan_for_pii.return_value = MagicMock()
    _setup_app_state(downstream_client, governance_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, downstream_client, governance_client
