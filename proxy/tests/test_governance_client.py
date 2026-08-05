from __future__ import annotations

import httpx
import pytest
from proxy.app.governance_client import (
    GovernanceClient,
    GovernanceError,
    InspectRequest,
)


@pytest.mark.asyncio
async def test_governance_client_preserves_retry_after_from_503():
    def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        governance = GovernanceClient(client, "internal-token")
        with pytest.raises(GovernanceError) as exc_info:
            await governance.inspect(
                InspectRequest(
                    text="sensitive text",
                    tenant_id="tenant",
                    user_id="user",
                    model_id="model",
                    routing_method="direct",
                )
            )

    assert exc_info.value.retry_after == "120"
    assert "sensitive text" not in str(exc_info.value)
