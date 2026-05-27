import httpx
from starlette.responses import Response, StreamingResponse

OPENAI_BASE = "https://api.openai.com/v1"


def make_client(api_key: str) -> httpx.AsyncClient:
    """Create the shared client. Call once at lifespan startup."""
    return httpx.AsyncClient(
        base_url=OPENAI_BASE,
        headers={"Authorization": f"Bearer {api_key}"},
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
    )


async def chat_completions(
    client: httpx.AsyncClient,
    body: dict,
    stream: bool,
    extra_headers: dict[str, str],
) -> Response | StreamingResponse:
    """Forward to OpenAI. Returns a Starlette Response."""
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
