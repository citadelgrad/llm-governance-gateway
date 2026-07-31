from fastapi import FastAPI

# Request-handling/MCP protocol logic is added by ai-gateway-h04.2; this
# service only needs to build and expose a healthcheck until then.
app = FastAPI(title="MCP Reverse Proxy")


@app.get("/health")
async def health():
    return {"status": "ok"}
