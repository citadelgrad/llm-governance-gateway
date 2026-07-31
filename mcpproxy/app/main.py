from __future__ import annotations

import json
import os
import secrets
import sys
import unicodedata
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from mcpproxy.app.circuit_breaker import CircuitBreaker
from mcpproxy.app.config import settings
from mcpproxy.app.governance_client import (
    AuditEventError,
    DlpScanError,
    GovernanceClient,
    make_governance_client,
)
from mcpproxy.app.opa_client import OpaCheckError, OpaClient, make_opa_client
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
    opa_http_client = httpx.AsyncClient(timeout=30.0)
    app.state.downstream_client = downstream_client
    app.state.governance_client = make_governance_client(governance_http_client)
    app.state.opa_client = make_opa_client(opa_http_client)
    app.state.circuit_breaker = CircuitBreaker()
    yield
    await downstream_client.aclose()
    await governance_http_client.aclose()
    await opa_http_client.aclose()


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


def _policy_violation_response() -> Response:
    body = json.dumps(
        {"error": {"type": "policy_violation", "message": "Tool call denied by policy"}}
    )
    return Response(content=body, status_code=403, media_type="application/json")


def _breakglass_allowed(tool: dict) -> bool:
    """Static, read-only fallback used only while the OPA Sidecar circuit
    breaker is open - never the entitlement-matrix document (unreachable
    through the down sidecar) and never the caller's coarse token scope."""
    key = f"{tool.get('server', '')}:{tool.get('name', '')}"
    return key in settings.breakglass_tool_allowlist


def _normalize_context(context: dict) -> dict:
    """NFC-normalizes context.resource before the OPA Sidecar input is
    built - Rego's canonicalize() handles lowercasing/trailing-slash
    stripping but has no Unicode-normalization builtin (see
    docs/auth-architecture.md, "Resource-string canonicalization")."""
    resource = context.get("resource")
    if not isinstance(resource, str):
        return context
    normalized = unicodedata.normalize("NFC", resource)
    if normalized == resource:
        return context
    return {**context, "resource": normalized}


@app.post("/v1/mcp/call")
async def call(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    """Receiving endpoint for calls forwarded by the Gateway Proxy.

    Requires a valid X-Internal-Token, matching the pattern used by every
    internal endpoint in governance/app/main.py - without this, a caller's
    self-declared `principal` (tenant_id/user_id) would be trusted straight
    from the request body with no verification of who is actually calling.

    Every call is checked against the OPA Sidecar's tool-call-boundary
    policy first, fresh on every call (no decision caching) - a deny blocks
    the call before it ever reaches the downstream MCP server. Only on an
    explicit allow does execution continue.

    While the sidecar is reachable, a per-replica circuit breaker tracks
    consecutive transport failures (never DENY decisions). After 5 such
    failures it opens, and calls fall back to a static, read-only tool
    allow-list instead of the sidecar - the break-glass path from
    docs/auth-architecture.md. After 30s open, exactly one call probes the
    sidecar again; success closes the breaker, failure reopens it and
    restarts the cooldown.

    Buffers the downstream MCP server's tool response in full - whether sent
    as a single JSON-RPC message or as SSE chunks - enforcing the 1 MiB /
    10s caps before anything is forwarded to the caller. The buffered
    response is then scanned by Governance's DLP checkpoint before being
    forwarded; a cap breach or scan failure blocks the response outright
    (fail-closed), unlike the harm-scan pipeline it deliberately diverges
    from.
    """
    if not x_internal_token or not secrets.compare_digest(
        x_internal_token, settings.governance_internal_token
    ):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Internal-Token")

    body = await request.json()
    principal = body.get("principal", {})
    tenant_id = principal.get("tenant_id", "")
    user_id = principal.get("user_id", "")
    tool = body.get("tool", {})
    context = _normalize_context(body.get("context") or {})

    client: httpx.AsyncClient = request.app.state.downstream_client
    governance_client: GovernanceClient = request.app.state.governance_client
    opa_client: OpaClient = request.app.state.opa_client
    circuit_breaker: CircuitBreaker = request.app.state.circuit_breaker

    is_probe = False
    used_breakglass = False
    if circuit_breaker.is_closed():
        call_sidecar = True
    elif circuit_breaker.try_start_probe():
        call_sidecar = True
        is_probe = True
    else:
        call_sidecar = False

    if call_sidecar:
        try:
            allowed = await opa_client.check_tool_call(
                principal=principal,
                actor=body.get("actor", {}),
                tool=tool,
                context=context,
            )
        except OpaCheckError:
            if is_probe:
                circuit_breaker.record_probe_failure()
                allowed = _breakglass_allowed(tool)
                used_breakglass = True
            else:
                circuit_breaker.record_failure()
                allowed = False
        else:
            circuit_breaker.record_success()
    else:
        allowed = _breakglass_allowed(tool)
        used_breakglass = True

    if not allowed:
        await _send_audit_event(
            governance_client,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="breakglass_denied" if used_breakglass else "policy_denied",
            decision="block",
        )
        return _policy_violation_response()

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
        scan_result = await governance_client.scan_for_pii(text)
    except (DlpScanError, UnicodeDecodeError):
        await _send_audit_event(
            governance_client,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="dlp_blocked",
            decision="block",
        )
        return Response(status_code=502)

    if scan_result.pii_findings:
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
        event_type="breakglass_tool_call" if used_breakglass else "mcp_tool_call",
        decision="allow",
    )
    return Response(content=buffered, status_code=200, media_type=content_type)
