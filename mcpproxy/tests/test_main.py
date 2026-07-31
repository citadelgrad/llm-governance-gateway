from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import mcpproxy.app.main as main_module
from mcpproxy.app.governance_client import DlpScanError
from mcpproxy.app.transport import ToolResponseTimeoutError, ToolResponseTooLargeError


def _make_stream_ctx(chunks: list[bytes], content_type: str = "application/json"):
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.headers = {"content-type": content_type}

    async def aiter_bytes():
        for chunk in chunks:
            yield chunk

    upstream.aiter_bytes = aiter_bytes

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=upstream)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _principal_body(tenant_id: str = "tenant-1", user_id: str = "user-1") -> dict:
    return {
        "tool": {"name": "echo"},
        "principal": {"tenant_id": tenant_id, "user_id": user_id, "roles": []},
    }


async def test_successful_json_rpc_response_is_forwarded_once_buffered(async_client):
    """AC1/AC3/AC6: single JSON-RPC chunk, buffered fully, scanned, then forwarded."""
    client, downstream_client, governance_client = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b'{"result": "ok"}']))

    resp = await client.post("/v1/mcp/call", json=_principal_body())

    assert resp.status_code == 200
    assert resp.content == b'{"result": "ok"}'
    governance_client.scan_for_pii.assert_awaited_once_with('{"result": "ok"}')
    governance_client.send_audit_event.assert_awaited_once_with(
        tenant_id="tenant-1", user_id="user-1", event_type="mcp_tool_call", decision="allow"
    )


async def test_multi_chunk_sse_response_is_fully_buffered_before_forwarding(async_client):
    """AC1: SSE-style chunked response is concatenated and forwarded as one body."""
    client, downstream_client, governance_client = async_client
    downstream_client.stream = MagicMock(
        return_value=_make_stream_ctx([b"event: a\n", b"data: b\n", b"\n"])
    )

    resp = await client.post("/v1/mcp/call", json=_principal_body())

    assert resp.status_code == 200
    assert resp.content == b"event: a\ndata: b\n\n"
    governance_client.scan_for_pii.assert_awaited_once_with("event: a\ndata: b\n\n")


async def test_oversized_response_fails_closed_with_no_partial_bytes(async_client):
    """AC5: a response exceeding the 1 MiB cap never reaches the scan call and is blocked."""
    client, downstream_client, governance_client = async_client

    async def big_chunks():
        yield b"x" * (2 * 1024 * 1024)

    ctx = MagicMock()
    upstream = MagicMock()
    upstream.headers = {"content-type": "application/json"}
    upstream.aiter_bytes = big_chunks
    ctx.__aenter__ = AsyncMock(return_value=upstream)
    ctx.__aexit__ = AsyncMock(return_value=False)
    downstream_client.stream = MagicMock(return_value=ctx)

    resp = await client.post("/v1/mcp/call", json=_principal_body())

    assert resp.status_code == 502
    assert resp.content == b""
    governance_client.scan_for_pii.assert_not_awaited()
    governance_client.send_audit_event.assert_awaited_once_with(
        tenant_id="tenant-1", user_id="user-1", event_type="dlp_blocked", decision="block"
    )


async def test_timed_out_response_fails_closed_with_no_partial_bytes(async_client, monkeypatch):
    """AC5: a response still incomplete after the wall-clock cap never reaches the scan call."""
    client, downstream_client, governance_client = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b"partial"]))

    async def _raise_timeout(*args, **kwargs):
        raise ToolResponseTimeoutError("simulated timeout")

    monkeypatch.setattr(main_module, "buffer_tool_response", _raise_timeout)

    resp = await client.post("/v1/mcp/call", json=_principal_body())

    assert resp.status_code == 502
    assert resp.content == b""
    governance_client.scan_for_pii.assert_not_awaited()
    governance_client.send_audit_event.assert_awaited_once_with(
        tenant_id="tenant-1", user_id="user-1", event_type="dlp_blocked", decision="block"
    )


async def test_too_large_error_from_buffering_fails_closed(async_client, monkeypatch):
    """AC5: ToolResponseTooLargeError raised mid-buffering also fails closed."""
    client, downstream_client, governance_client = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b"partial"]))

    async def _raise_too_large(*args, **kwargs):
        raise ToolResponseTooLargeError("simulated overflow")

    monkeypatch.setattr(main_module, "buffer_tool_response", _raise_too_large)

    resp = await client.post("/v1/mcp/call", json=_principal_body())

    assert resp.status_code == 502
    assert resp.content == b""
    governance_client.scan_for_pii.assert_not_awaited()
    governance_client.send_audit_event.assert_awaited_once_with(
        tenant_id="tenant-1", user_id="user-1", event_type="dlp_blocked", decision="block"
    )


async def test_dlp_scan_error_blocks_response_and_sends_dlp_blocked_audit(async_client):
    """AC4: a scan call that errors blocks the response outright, never forwarding it."""
    client, downstream_client, governance_client = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b'{"result": "ok"}']))
    governance_client.scan_for_pii.side_effect = DlpScanError("simulated scan failure")

    resp = await client.post("/v1/mcp/call", json=_principal_body())

    assert resp.status_code == 502
    assert resp.content == b""
    governance_client.send_audit_event.assert_awaited_once_with(
        tenant_id="tenant-1", user_id="user-1", event_type="dlp_blocked", decision="block"
    )


async def test_dlp_scan_only_sends_text_field(async_client):
    """AC2: the DLP scan call receives only {"text": str} - no llm/authz-shaped fields."""
    client, downstream_client, governance_client = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b'{"result": "ok"}']))

    await client.post("/v1/mcp/call", json=_principal_body())

    assert governance_client.scan_for_pii.await_args.args == ('{"result": "ok"}',)
    assert governance_client.scan_for_pii.await_args.kwargs == {}


async def test_non_utf8_response_fails_closed(async_client):
    """A non-UTF-8-decodable buffered response can't be scanned, so it fails closed."""
    client, downstream_client, governance_client = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b"\xff\xfe\xfa"]))

    resp = await client.post("/v1/mcp/call", json=_principal_body())

    assert resp.status_code == 502
    assert resp.content == b""
    governance_client.scan_for_pii.assert_not_awaited()
    governance_client.send_audit_event.assert_awaited_once_with(
        tenant_id="tenant-1", user_id="user-1", event_type="dlp_blocked", decision="block"
    )
