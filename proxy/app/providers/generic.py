"""Generic OpenAI-compatible pass-through adapter for unknown providers with a base_url."""

import httpx
from starlette.responses import Response, StreamingResponse


def make_client() -> httpx.AsyncClient:
    """Create the shared client. No base_url or auth — both supplied per request."""
    return httpx.AsyncClient(
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
    )


async def chat_completions(
    client: httpx.AsyncClient,
    body: dict,
    stream: bool,
    extra_headers: dict[str, str],
    *,
    base_url: str,
    api_key: str = "",
) -> Response | StreamingResponse:
    """Forward to an OpenAI-compatible endpoint. Returns a Starlette Response."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    auth_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    if stream:
        try:
            async def _stream_body(upstream_ctx):
                async with upstream_ctx as upstream:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk

            req = client.stream("POST", url, json=body, headers=auth_headers)
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
            upstream = await client.post(url, json=body, headers=auth_headers)
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
