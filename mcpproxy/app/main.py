from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
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
    app.state.downstream_client = downstream_client
    yield
    await downstream_client.aclose()


app = FastAPI(title="MCP Reverse Proxy", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/mcp/call")
async def call(request: Request):
    """Receiving endpoint for calls forwarded by the Gateway Proxy.

    Buffers the downstream MCP server's tool response in full - whether sent
    as a single JSON-RPC message or as SSE chunks - enforcing the 1 MiB /
    10s caps before anything is forwarded to the caller. OPA sidecar
    evaluation, DLP scanning, and break-glass are added by later
    ai-gateway-h04 tasks.
    """
    body = await request.json()
    client: httpx.AsyncClient = request.app.state.downstream_client

    try:
        async with client.stream("POST", DOWNSTREAM_URL, json=body) as upstream:
            buffered = await buffer_tool_response(upstream.aiter_bytes())
            content_type = upstream.headers.get("content-type", "application/json")
    except (ToolResponseTooLargeError, ToolResponseTimeoutError):
        return Response(status_code=502)

    return Response(content=buffered, status_code=200, media_type=content_type)
