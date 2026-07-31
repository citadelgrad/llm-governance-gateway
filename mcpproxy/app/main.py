from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from mcpproxy.app.governance_client import (
    AuditEventError,
    DlpScanError,
    GovernanceClient,
    make_governance_client,
)
from mcpproxy.app.transport import (
    ToolResponseTimeoutError,
    ToolResponseTooLargeError,
    buffer_tool_response,
)

# No downstream-MCP-server registry exists yet (per-`tool.server` resolution is
# a separate, not-yet-scheduled concern) - every call is routed through a
# single configurable downstream target until real dispatch resolution lands.
DOWNSTREAM_URL = os.environ.get("MCP_DOWNSTREAM_URL", "http://localhost:9999/mcp")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # timeout is deliberately looser than TOOL_RESPONSE_TIMEOUT_SECONDS so our
    # own wall-clock enforcement in buffer_tool_response is what fires first.
    downstream_client = httpx.AsyncClient(timeout=30.0)
    governance_http_client = httpx.AsyncClient(timeout=30.0)
    app.state.downstream_client = downstream_client
    app.state.governance_client = make_governance_client(governance_http_client)
    yield
    await downstream_client.aclose()
    await governance_http_client.aclose()


app = FastAPI(title="MCP Reverse Proxy", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


async def _send_audit_event(
    governance_client: GovernanceClient,
    *,
    tenant_id: str,
    user_id: str,
    event_type: str,
    decision: str,
) -> None:
    """Best-effort: a failed audit write is logged, not raised - it must never
    overturn the block/forward decision the DLP scan already determined."""
    try:
        await governance_client.send_audit_event(
            tenant_id=tenant_id, user_id=user_id, event_type=event_type, decision=decision
        )
    except AuditEventError as exc:
        print(f"[mcpproxy] {exc}", file=sys.stderr)


@app.post("/v1/mcp/call")
async def call(request: Request):
    """Receiving endpoint for calls forwarded by the Gateway Proxy.

    Buffers the downstream MCP server's tool response in full - whether sent
    as a single JSON-RPC message or as SSE chunks - enforcing the 1 MiB /
    10s caps before anything is forwarded to the caller. The buffered
    response is then scanned by Governance's DLP checkpoint before being
    forwarded; a cap breach or scan failure blocks the response outright
    (fail-closed), unlike the harm-scan pipeline it deliberately diverges
    from. OPA sidecar evaluation and break-glass are added by later
    ai-gateway-h04 tasks.
    """
    body = await request.json()
    principal = body.get("principal", {})
    tenant_id = principal.get("tenant_id", "")
    user_id = principal.get("user_id", "")

    client: httpx.AsyncClient = request.app.state.downstream_client
    governance_client: GovernanceClient = request.app.state.governance_client

    try:
        async with client.stream("POST", DOWNSTREAM_URL, json=body) as upstream:
            buffered = await buffer_tool_response(upstream.aiter_bytes())
            content_type = upstream.headers.get("content-type", "application/json")
    except (ToolResponseTooLargeError, ToolResponseTimeoutError):
        await _send_audit_event(
            governance_client,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="dlp_blocked",
            decision="block",
        )
        return Response(status_code=502)

    try:
        text = buffered.decode("utf-8")
        await governance_client.scan_for_pii(text)
    except (DlpScanError, UnicodeDecodeError):
        await _send_audit_event(
            governance_client,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="dlp_blocked",
            decision="block",
        )
        return Response(status_code=502)

    await _send_audit_event(
        governance_client,
        tenant_id=tenant_id,
        user_id=user_id,
        event_type="mcp_tool_call",
        decision="allow",
    )
    return Response(content=buffered, status_code=200, media_type=content_type)
