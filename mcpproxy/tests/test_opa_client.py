from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcpproxy.app.opa_client import OpaCheckError, OpaClient


def _mock_http_client(response: MagicMock) -> AsyncMock:
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    return client


async def test_allow_result_true_returns_true():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"result": True}
    opa_client = OpaClient(_mock_http_client(resp))

    allowed = await opa_client.check_tool_call(principal={}, actor={}, tool={}, context={})

    assert allowed is True


async def test_allow_result_false_returns_false():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"result": False}
    opa_client = OpaClient(_mock_http_client(resp))

    allowed = await opa_client.check_tool_call(principal={}, actor={}, tool={}, context={})

    assert allowed is False


async def test_non_2xx_response_raises_opa_check_error():
    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=Exception("503 Service Unavailable"))
    opa_client = OpaClient(_mock_http_client(resp))

    with pytest.raises(OpaCheckError):
        await opa_client.check_tool_call(principal={}, actor={}, tool={}, context={})


async def test_malformed_json_body_raises_opa_check_error_not_unhandled_exception():
    """A sidecar response that passes raise_for_status but isn't valid JSON
    must still be wrapped as OpaCheckError (fail-closed), not escape as a raw
    exception that would bypass the circuit breaker's failure accounting."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(side_effect=ValueError("not valid json"))
    opa_client = OpaClient(_mock_http_client(resp))

    with pytest.raises(OpaCheckError):
        await opa_client.check_tool_call(principal={}, actor={}, tool={}, context={})


async def test_missing_result_key_raises_opa_check_error():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"unexpected": "shape"}
    opa_client = OpaClient(_mock_http_client(resp))

    with pytest.raises(OpaCheckError):
        await opa_client.check_tool_call(principal={}, actor={}, tool={}, context={})
