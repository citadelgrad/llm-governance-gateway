from fastapi import FastAPI, Request

app = FastAPI(title="MCP Reverse Proxy")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/mcp/call")
async def call(request: Request):
    """Scaffold receiving endpoint for calls forwarded by the Gateway Proxy.

    OPA sidecar evaluation, response buffering, DLP scanning, and break-glass
    are added by later ai-gateway-h04 tasks; this only accepts the forwarded
    request and echoes the tool identity back.
    """
    body = await request.json()
    return {"status": "accepted", "tool": body.get("tool", {})}
