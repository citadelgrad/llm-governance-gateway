import httpx
from proxy.app.protocol_types import JsonObject
from proxy.app.providers.errors import sanitize_upstream_error
from proxy.app.providers.native import forward_native
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


async def responses(
    client: httpx.AsyncClient,
    body: JsonObject,
    stream: bool,
    extra_headers: dict[str, str],
    upstream_headers: dict[str, str] | None = None,
) -> Response | StreamingResponse:
    """Forward a validated OpenAI Responses request without Chat translation."""
    return await forward_native(
        client,
        path="/responses",
        body=body,
        stream=stream,
        extra_headers=extra_headers,
        provider="openai",
        upstream_headers=upstream_headers,
    )


async def chat_completions(
    client: httpx.AsyncClient,
    body: JsonObject,
    stream: bool,
    extra_headers: dict[str, str],
) -> Response | StreamingResponse:
    """Forward to OpenAI. Returns a Starlette Response."""
    upstream_body = body
    if stream:
        request = client.build_request("POST", "/chat/completions", json=upstream_body)
        try:
            upstream = await client.send(request, stream=True)
        except httpx.TimeoutException:
            return Response(content=b"upstream timeout", status_code=504)
        except httpx.RequestError:
            return Response(content=b"upstream connection error", status_code=502)

        if upstream.status_code != 200:
            await upstream.aread()
            response = sanitize_upstream_error(upstream, extra_headers, provider="openai")
            await upstream.aclose()
            return response

        async def _stream_body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            except httpx.TimeoutException:
                yield b'data: {"error": {"type": "upstream_timeout"}}\n\n'
            except httpx.RequestError:
                yield b'data: {"error": {"type": "upstream_connection_error"}}\n\n'
            finally:
                await upstream.aclose()

        return StreamingResponse(
            _stream_body(),
            status_code=200,
            media_type="text/event-stream",
            headers=extra_headers,
        )

    try:
        upstream = await client.post("/chat/completions", json=upstream_body)
    except httpx.TimeoutException:
        return Response(content=b"upstream timeout", status_code=504)
    except httpx.RequestError:
        return Response(content=b"upstream connection error", status_code=502)

    if upstream.status_code != 200:
        return sanitize_upstream_error(upstream, extra_headers, provider="openai")

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=extra_headers,
    )
