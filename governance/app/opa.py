from __future__ import annotations

from dataclasses import dataclass, field

import httpx


class OPAError(Exception):
    """Base class for OPA errors."""

class OPATimeoutError(OPAError):
    """OPA did not respond within the timeout window."""

class OPAConnectionError(OPAError):
    """Cannot reach OPA (fail-closed)."""

class OPAResultError(OPAError):
    """OPA response missing required keys."""


@dataclass
class OPAResult:
    allowed: bool
    violations: list[str] = field(default_factory=list)


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


async def close_client() -> None:
    """Task 9: close the module-level httpx client on shutdown to avoid resource leaks."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def check(opa_url: str, input_data: dict) -> OPAResult:
    """
    Query OPA /v1/data/llm/authz. Fail-closed on any error.
    deny wins even when allow=True (deny+allow coexistence -> block).
    """
    try:
        client = _get_client()
        resp = await client.post(
            f"{opa_url}/v1/data/llm/authz",
            json={"input": input_data},
        )
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise OPATimeoutError("OPA request timed out (fail-closed)") from exc
    except httpx.RequestError as exc:
        raise OPAConnectionError(f"Cannot reach OPA at {opa_url} (fail-closed)") from exc
    except httpx.HTTPStatusError as exc:
        # Task 8: catch 4xx/5xx so they route through OPAError hierarchy in pipeline
        raise OPAResultError(f"OPA returned HTTP {exc.response.status_code} (fail-closed)") from exc

    body = resp.json()
    if "result" not in body:
        raise OPAResultError("OPA response missing 'result' key (fail-closed)")

    result = body["result"]
    allow: bool = result.get("allow", False)
    deny: list[str] = result.get("deny", [])

    # deny wins even when allow=True
    allowed = allow and len(deny) == 0
    return OPAResult(allowed=allowed, violations=list(deny))
