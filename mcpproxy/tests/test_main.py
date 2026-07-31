from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import mcpproxy.app.main as main_module
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


async def test_successful_json_rpc_response_is_forwarded_once_buffered(async_client):
    """AC1/AC3/AC6: single JSON-RPC chunk, buffered fully, forwarded once complete."""
    client, downstream_client = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b'{"result": "ok"}']))

    resp = await client.post("/v1/mcp/call", json={"tool": {"name": "echo"}})

    assert resp.status_code == 200
    assert resp.content == b'{"result": "ok"}'


async def test_multi_chunk_sse_response_is_fully_buffered_before_forwarding(async_client):
    """AC1: SSE-style chunked response is concatenated and forwarded as one body."""
    client, downstream_client = async_client
    downstream_client.stream = MagicMock(
        return_value=_make_stream_ctx([b"event: a\n", b"data: b\n", b"\n"])
    )

    resp = await client.post("/v1/mcp/call", json={"tool": {"name": "echo"}})

    assert resp.status_code == 200
    assert resp.content == b"event: a\ndata: b\n\n"


async def test_oversized_response_fails_closed_with_no_partial_bytes(async_client):
    """AC2/AC5: a response exceeding the 1 MiB cap never reaches the caller."""
    client, downstream_client = async_client

    async def big_chunks():
        yield b"x" * (2 * 1024 * 1024)

    ctx = MagicMock()
    upstream = MagicMock()
    upstream.headers = {"content-type": "application/json"}
    upstream.aiter_bytes = big_chunks
    ctx.__aenter__ = AsyncMock(return_value=upstream)
    ctx.__aexit__ = AsyncMock(return_value=False)
    downstream_client.stream = MagicMock(return_value=ctx)

    resp = await client.post("/v1/mcp/call", json={"tool": {"name": "echo"}})

    assert resp.status_code == 502
    assert resp.content == b""


async def test_timed_out_response_fails_closed_with_no_partial_bytes(async_client, monkeypatch):
    """AC4: a response still incomplete after the wall-clock cap never reaches the caller."""
    client, downstream_client = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b"partial"]))

    async def _raise_timeout(*args, **kwargs):
        raise ToolResponseTimeoutError("simulated timeout")

    monkeypatch.setattr(main_module, "buffer_tool_response", _raise_timeout)

    resp = await client.post("/v1/mcp/call", json={"tool": {"name": "echo"}})

    assert resp.status_code == 502
    assert resp.content == b""


async def test_too_large_error_from_buffering_fails_closed(async_client, monkeypatch):
    """AC5: ToolResponseTooLargeError raised mid-buffering also fails closed."""
    client, downstream_client = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b"partial"]))

    async def _raise_too_large(*args, **kwargs):
        raise ToolResponseTooLargeError("simulated overflow")

    monkeypatch.setattr(main_module, "buffer_tool_response", _raise_too_large)

    resp = await client.post("/v1/mcp/call", json={"tool": {"name": "echo"}})

    assert resp.status_code == 502
    assert resp.content == b""
