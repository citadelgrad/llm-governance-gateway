from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import mcpproxy.app.main as main_module
from mcpproxy.app.governance_client import DlpScanError
from mcpproxy.app.opa_client import OpaCheckError
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


def _full_call_body() -> dict:
    return {
        "principal": {
            "user_id": "user-1",
            "tenant_id": "tenant-1",
            "roles": ["mcp-role:github-write"],
        },
        "actor": {"agent_id": "agent-1", "session_id": "sess-1"},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name"},
    }


async def test_opa_input_document_matches_doc_shape(async_client):
    """AC1: the OPA Sidecar input document carries principal, actor, tool, and context."""
    client, downstream_client, _ = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b'{"result": "ok"}']))
    opa_client = main_module.app.state.opa_client
    body = _full_call_body()

    await client.post("/v1/mcp/call", json=body)

    opa_client.check_tool_call.assert_awaited_once_with(
        principal=body["principal"],
        actor=body["actor"],
        tool=body["tool"],
        context=body["context"],
    )


async def test_opa_input_defaults_missing_actor_and_context_to_empty_dict(async_client):
    """AC1: actor/context are always present in the input document, even when the
    Gateway Proxy's request body omits them (actor has no data source upstream yet)."""
    client, downstream_client, _ = async_client
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b'{"result": "ok"}']))
    opa_client = main_module.app.state.opa_client

    await client.post("/v1/mcp/call", json=_principal_body())

    opa_client.check_tool_call.assert_awaited_once_with(
        principal={"tenant_id": "tenant-1", "user_id": "user-1", "roles": []},
        actor={},
        tool={"name": "echo"},
        context={},
    )


async def test_opa_deny_blocks_call_sends_policy_denied_audit_and_403(async_client):
    """AC5: a deny never reaches the downstream call, sends policy_denied, and
    returns a policy-violation response - not a partial/forwarded tool result."""
    client, downstream_client, governance_client = async_client
    opa_client = main_module.app.state.opa_client
    opa_client.check_tool_call.return_value = False

    resp = await client.post("/v1/mcp/call", json=_full_call_body())

    assert resp.status_code == 403
    assert json.loads(resp.content)["error"]["type"] == "policy_violation"
    downstream_client.stream.assert_not_called()
    governance_client.send_audit_event.assert_awaited_once_with(
        tenant_id="tenant-1", user_id="user-1", event_type="policy_denied", decision="block"
    )


async def test_opa_check_error_fails_closed_like_a_deny(async_client):
    """An unreachable/erroring sidecar is treated exactly like an explicit deny -
    fail-closed, since the break-glass path (ai-gateway-h04.11) doesn't exist yet."""
    client, downstream_client, governance_client = async_client
    opa_client = main_module.app.state.opa_client
    opa_client.check_tool_call.side_effect = OpaCheckError("simulated sidecar outage")

    resp = await client.post("/v1/mcp/call", json=_full_call_body())

    assert resp.status_code == 403
    downstream_client.stream.assert_not_called()
    governance_client.send_audit_event.assert_awaited_once_with(
        tenant_id="tenant-1", user_id="user-1", event_type="policy_denied", decision="block"
    )


async def test_opa_allow_proceeds_to_downstream_call(async_client):
    """AC6: an explicit allow lets the call continue to the downstream MCP server."""
    client, downstream_client, _ = async_client
    opa_client = main_module.app.state.opa_client
    opa_client.check_tool_call.return_value = True
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b'{"result": "ok"}']))

    resp = await client.post("/v1/mcp/call", json=_full_call_body())

    assert resp.status_code == 200
    downstream_client.stream.assert_called_once()


async def test_opa_evaluates_fresh_on_every_call_no_caching(async_client):
    """AC7: two identical, back-to-back calls each trigger their own sidecar
    evaluation - the second call's decision is never reused from the first."""
    client, downstream_client, _ = async_client
    opa_client = main_module.app.state.opa_client
    downstream_client.stream = MagicMock(
        side_effect=lambda *a, **k: _make_stream_ctx([b'{"result": "ok"}'])
    )
    body = _full_call_body()

    await client.post("/v1/mcp/call", json=body)
    await client.post("/v1/mcp/call", json=body)

    assert opa_client.check_tool_call.await_count == 2


def test_opa_sidecar_url_defaults_to_loopback():
    """AC8: the sidecar is reached over loopback, not a routed network hop."""
    from mcpproxy.app.config import settings

    assert settings.opa_sidecar_url.startswith("http://127.0.0.1:")
