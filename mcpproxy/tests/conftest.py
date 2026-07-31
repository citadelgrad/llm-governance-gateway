from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from mcpproxy.app.circuit_breaker import CircuitBreaker
from mcpproxy.app.config import settings
from mcpproxy.app.governance_client import DlpScanResult
from mcpproxy.app.main import app


def _setup_app_state(downstream_client, governance_client, opa_client):
    """Set app.state directly - ASGITransport does not fire the ASGI lifespan."""
    app.state.downstream_client = downstream_client
    app.state.governance_client = governance_client
    app.state.opa_client = opa_client
    app.state.circuit_breaker = CircuitBreaker()


@pytest.fixture
async def async_client():
    """ASGI client with mocked downstream, governance, and OPA clients on app.state.

    governance_client.scan_for_pii defaults to a successful no-op scan, and
    opa_client.check_tool_call defaults to True (allow), so existing
    forward/block assertions around buffering don't also need to stub the
    DLP or OPA checkpoints; tests exercising a checkpoint itself override
    that mock's return value/side effect. opa_client is set on app.state but
    not part of the yielded tuple - tests that need it reach it via
    `main_module.app.state.opa_client`, keeping this fixture's yield shape
    unchanged for every test written before the OPA checkpoint existed.
    """
    downstream_client = AsyncMock()
    governance_client = AsyncMock()
    governance_client.scan_for_pii.return_value = DlpScanResult(
        pii_findings=[], data_classification="none", redacted_text=""
    )
    opa_client = AsyncMock()
    opa_client.check_tool_call.return_value = True
    _setup_app_state(downstream_client, governance_client, opa_client)

    headers = {"X-Internal-Token": settings.governance_internal_token}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    ) as client:
        yield client, downstream_client, governance_client
