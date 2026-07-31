from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import mcpproxy.app.circuit_breaker as circuit_breaker_module
import mcpproxy.app.main as main_module
from mcpproxy.app.circuit_breaker import CircuitState
from mcpproxy.app.governance_client import DlpScanError
from mcpproxy.app.opa_client import OpaCheckError
from mcpproxy.app.transport import ToolResponseTimeoutError, ToolResponseTooLargeError


class _FakeClock:
    """Controllable stand-in for time.monotonic() - avoids sleeping 30 real
    seconds in tests that exercise the circuit breaker's cooldown."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _open_the_breaker(circuit_breaker) -> None:
    for _ in range(5):
        circuit_breaker.record_failure()


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
    """A single sidecar failure (below the 5-failure breaker threshold) is
    treated exactly like an explicit deny - fail-closed."""
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


def _github_list_prs_body() -> dict:
    """A tool call whose (server, tool) pair is on the seeded break-glass
    allow-list (`"github-mcp:list_prs"`, see mcpproxy/app/config.py)."""
    body = _full_call_body()
    body["tool"] = {"server": "github-mcp", "name": "list_prs", "arguments": {}}
    return body


async def test_five_consecutive_sidecar_failures_trip_the_breaker(async_client):
    """AC1/AC2: 5 consecutive OpaCheckError failures open the breaker."""
    client, _downstream_client, _governance_client = async_client
    opa_client = main_module.app.state.opa_client
    circuit_breaker = main_module.app.state.circuit_breaker
    opa_client.check_tool_call.side_effect = OpaCheckError("simulated outage")

    for _ in range(5):
        await client.post("/v1/mcp/call", json=_full_call_body())

    assert circuit_breaker.state is CircuitState.OPEN


async def test_deny_decisions_never_count_toward_the_failure_threshold(async_client):
    """AC1: repeated explicit denies (no exception) never trip the breaker,
    since a deny is a successful round trip, not a counted failure."""
    client, _downstream_client, _governance_client = async_client
    opa_client = main_module.app.state.opa_client
    circuit_breaker = main_module.app.state.circuit_breaker
    opa_client.check_tool_call.return_value = False

    for _ in range(10):
        await client.post("/v1/mcp/call", json=_full_call_body())

    assert circuit_breaker.is_closed()


async def test_an_intervening_deny_resets_the_consecutive_failure_streak(async_client):
    """AC1/AC2: 4 failures, then a deny, then 4 more failures never trips the
    breaker - "consecutive" resets on any successful round trip."""
    client, _downstream_client, _governance_client = async_client
    opa_client = main_module.app.state.opa_client
    circuit_breaker = main_module.app.state.circuit_breaker

    opa_client.check_tool_call.side_effect = OpaCheckError("simulated outage")
    for _ in range(4):
        await client.post("/v1/mcp/call", json=_full_call_body())

    opa_client.check_tool_call.side_effect = None
    opa_client.check_tool_call.return_value = False
    await client.post("/v1/mcp/call", json=_full_call_body())

    opa_client.check_tool_call.side_effect = OpaCheckError("simulated outage")
    for _ in range(4):
        await client.post("/v1/mcp/call", json=_full_call_body())

    assert circuit_breaker.is_closed()


async def test_call_denied_while_open_for_tool_not_on_allowlist(async_client):
    """AC3/AC4: while open, a tool not on the static allow-list is denied
    without any sidecar call, never consulting the entitlement matrix."""
    client, downstream_client, governance_client = async_client
    opa_client = main_module.app.state.opa_client
    circuit_breaker = main_module.app.state.circuit_breaker
    _open_the_breaker(circuit_breaker)
    opa_client.check_tool_call.reset_mock()

    resp = await client.post("/v1/mcp/call", json=_full_call_body())

    assert resp.status_code == 403
    opa_client.check_tool_call.assert_not_awaited()
    downstream_client.stream.assert_not_called()
    governance_client.send_audit_event.assert_awaited_once_with(
        tenant_id="tenant-1", user_id="user-1", event_type="policy_denied", decision="block"
    )


async def test_call_allowed_while_open_for_tool_on_allowlist(async_client):
    """AC3/AC5: while open, a tool on the static allow-list is allowed and
    proceeds to the downstream call without any sidecar call."""
    client, downstream_client, _governance_client = async_client
    opa_client = main_module.app.state.opa_client
    circuit_breaker = main_module.app.state.circuit_breaker
    _open_the_breaker(circuit_breaker)
    opa_client.check_tool_call.reset_mock()
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b'{"result": "ok"}']))

    resp = await client.post("/v1/mcp/call", json=_github_list_prs_body())

    assert resp.status_code == 200
    opa_client.check_tool_call.assert_not_awaited()
    downstream_client.stream.assert_called_once()


async def test_probe_after_cooldown_reaches_sidecar_and_closes_on_success(
    async_client, monkeypatch
):
    """AC6/AC7: after the 30s cooldown, a call probes the sidecar again; a
    successful probe closes the breaker immediately, no manual reset."""
    client, downstream_client, _governance_client = async_client
    opa_client = main_module.app.state.opa_client
    circuit_breaker = main_module.app.state.circuit_breaker
    clock = _FakeClock(start=1000.0)
    monkeypatch.setattr(circuit_breaker_module.time, "monotonic", clock)
    _open_the_breaker(circuit_breaker)  # opened_at stamped at clock.now == 1000.0
    opa_client.check_tool_call.reset_mock()
    opa_client.check_tool_call.return_value = True
    downstream_client.stream = MagicMock(return_value=_make_stream_ctx([b'{"result": "ok"}']))

    clock.now = 1031.0  # 31s later - past the 30s cooldown

    resp = await client.post("/v1/mcp/call", json=_full_call_body())

    assert resp.status_code == 200
    opa_client.check_tool_call.assert_awaited_once()
    assert circuit_breaker.is_closed()


async def test_only_one_probe_call_when_requests_race_after_cooldown(async_client, monkeypatch):
    """AC6: while one request's probe is in flight, a second concurrent
    request does not also reach the sidecar - it takes the allow-list path."""
    client, downstream_client, _governance_client = async_client
    opa_client = main_module.app.state.opa_client
    circuit_breaker = main_module.app.state.circuit_breaker
    clock = _FakeClock(start=2000.0)
    monkeypatch.setattr(circuit_breaker_module.time, "monotonic", clock)
    _open_the_breaker(circuit_breaker)
    clock.now = 2031.0

    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def slow_check_tool_call(**kwargs):
        probe_started.set()
        await release_probe.wait()
        return True

    opa_client.check_tool_call.side_effect = slow_check_tool_call
    downstream_client.stream = MagicMock(
        side_effect=lambda *a, **k: _make_stream_ctx([b'{"result": "ok"}'])
    )

    probe_task = asyncio.create_task(client.post("/v1/mcp/call", json=_full_call_body()))
    await probe_started.wait()

    second_resp = await client.post("/v1/mcp/call", json=_github_list_prs_body())
    release_probe.set()
    first_resp = await probe_task

    assert opa_client.check_tool_call.await_count == 1
    assert second_resp.status_code == 200
    assert first_resp.status_code == 200


async def test_failed_probe_reopens_breaker_and_restarts_the_cooldown(async_client, monkeypatch):
    """AC8: a failed probe reopens the breaker and restarts the 30s cooldown -
    an immediate follow-up call (no further time advance) does not re-probe."""
    client, _downstream_client, _governance_client = async_client
    opa_client = main_module.app.state.opa_client
    circuit_breaker = main_module.app.state.circuit_breaker
    clock = _FakeClock(start=3000.0)
    monkeypatch.setattr(circuit_breaker_module.time, "monotonic", clock)
    _open_the_breaker(circuit_breaker)
    clock.now = 3031.0
    opa_client.check_tool_call.side_effect = OpaCheckError("simulated probe failure")

    probe_resp = await client.post("/v1/mcp/call", json=_full_call_body())

    assert circuit_breaker.state is CircuitState.OPEN
    assert probe_resp.status_code == 403

    opa_client.check_tool_call.reset_mock()
    follow_up_resp = await client.post("/v1/mcp/call", json=_full_call_body())

    opa_client.check_tool_call.assert_not_awaited()
    assert follow_up_resp.status_code == 403
