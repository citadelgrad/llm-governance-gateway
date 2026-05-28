"""Ollama provider — OpenAI-compatible endpoint with configurable base URL."""

import httpx
from starlette.responses import Response, StreamingResponse

DEFAULT_OLLAMA_BASE = "http://localhost:11434/v1"


def make_client(base_url: str | None = None) -> httpx.AsyncClient:
    """Create the shared client. Call once at lifespan startup."""
    return httpx.AsyncClient(
        base_url=base_url or DEFAULT_OLLAMA_BASE,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(connect=5.0, read=240.0, write=10.0, pool=5.0),
    )


async def chat_completions(
    client: httpx.AsyncClient,
    body: dict,
    stream: bool,
    extra_headers: dict[str, str],
) -> Response | StreamingResponse:
    """Forward to Ollama. Returns a Starlette Response."""
    if stream:
        try:
            async def _stream_body(upstream_ctx):
                async with upstream_ctx as upstream:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk

            req = client.stream("POST", "/chat/completions", json=body)
            return StreamingResponse(
                _stream_body(req),
                status_code=200,
                media_type="text/event-stream",
                headers=extra_headers,
            )
        except httpx.TimeoutException:
            return Response(content=b"upstream timeout", status_code=504)
        except httpx.RequestError:
            return Response(content=b"upstream connection error", status_code=502)
    else:
        try:
            upstream = await client.post("/chat/completions", json=body)
        except httpx.TimeoutException:
            return Response(content=b"upstream timeout", status_code=504)
        except httpx.RequestError:
            return Response(content=b"upstream connection error", status_code=502)

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            headers=extra_headers,
        )
