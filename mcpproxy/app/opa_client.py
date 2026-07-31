from __future__ import annotations

import httpx
from mcpproxy.app.config import settings

# Loopback call to the colocated sidecar - a couple of seconds is generous
# headroom, not a network-hop timeout budget.
OPA_CHECK_TIMEOUT_SECONDS = 2.0


class OpaCheckError(Exception):
    """Raised on any OPA sidecar check failure. Callers must treat this as fail-closed (deny)."""


class OpaClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def check_tool_call(
        self, *, principal: dict, actor: dict, tool: dict, context: dict
    ) -> bool:
        """No retries, no caching: every call is a fresh HTTP POST (AC7)."""
        try:
            resp = await self._client.post(
                f"{settings.opa_sidecar_url}/v1/data/mcp/authz/allow",
                json={
                    "input": {
                        "principal": principal,
                        "actor": actor,
                        "tool": tool,
                        "context": context,
                    }
                },
                timeout=OPA_CHECK_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise OpaCheckError(f"OPA sidecar check failed: {exc}") from exc

        if "result" not in data:
            raise OpaCheckError("OPA sidecar response missing 'result' key")
        return bool(data["result"])


def make_opa_client(client: httpx.AsyncClient) -> OpaClient:
    return OpaClient(client)
