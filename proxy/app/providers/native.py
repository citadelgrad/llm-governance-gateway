"""Lossless forwarding for provider-native JSON and SSE protocol surfaces."""

from collections.abc import AsyncIterator

import httpx
from proxy.app.protocol_types import JsonObject
from proxy.app.providers.errors import sanitize_upstream_error
from starlette.responses import Response, StreamingResponse

_SAFE_UPSTREAM_RESPONSE_HEADERS = frozenset(
    {
        "anthropic-organization-id",
        "openai-organization",
        "openai-processing-ms",
        "openai-project",
        "request-id",
        "retry-after",
        "x-request-id",
    }
)


def _response_headers(upstream: httpx.Response, extra_headers: dict[str, str]) -> dict[str, str]:
    forwarded = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in _SAFE_UPSTREAM_RESPONSE_HEADERS
        or key.lower().startswith(("ratelimit-", "x-ratelimit-"))
    }
    forwarded.update(extra_headers)
    return forwarded


async def open_checked_stream(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    body: JsonObject,
    extra_headers: dict[str, str],
    provider: str,
    upstream_headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> tuple[httpx.Response | None, Response | None]:
    """Open an upstream stream and preserve pre-stream HTTP failures."""
    request = client.build_request(
        method,
        path,
        json=body,
        headers=upstream_headers,
        params=params,
    )
    try:
        upstream = await client.send(request, stream=True)
    except httpx.TimeoutException:
        return None, Response(content=b"upstream timeout", status_code=504, headers=extra_headers)
    except httpx.RequestError:
        return None, Response(
            content=b"upstream connection error", status_code=502, headers=extra_headers
        )
    if upstream.status_code != 200:
        await upstream.aread()
        error_response = sanitize_upstream_error(upstream, extra_headers, provider=provider)
        await upstream.aclose()
        return None, error_response
    content_type = upstream.headers.get("content-type", "").lower()
    if "text/event-stream" not in content_type:
        await upstream.aread()
        await upstream.aclose()
        return None, Response(
            content=b'{"error":{"type":"invalid_upstream_stream","message":"Upstream returned a non-SSE response"}}',
            status_code=502,
            media_type="application/json",
            headers=extra_headers,
        )
    return upstream, None


async def forward_native(
    client: httpx.AsyncClient,
    *,
    path: str,
    body: JsonObject,
    stream: bool,
    extra_headers: dict[str, str],
    provider: str,
    upstream_headers: dict[str, str] | None = None,
) -> Response | StreamingResponse:
    """Forward a validated native payload without translating or dropping fields."""
    if stream:
        upstream, error_response = await open_checked_stream(
            client,
            "POST",
            path,
            body=body,
            extra_headers=extra_headers,
            provider=provider,
            upstream_headers=upstream_headers,
        )
        if error_response is not None:
            return error_response
        assert upstream is not None

        async def stream_body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            except httpx.TimeoutException:
                yield b'event: error\ndata: {"error":{"type":"upstream_timeout"}}\n\n'
            except httpx.RequestError:
                yield b'event: error\ndata: {"error":{"type":"upstream_connection_error"}}\n\n'
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=200,
            media_type="text/event-stream",
            headers=_response_headers(upstream, extra_headers),
        )

    try:
        upstream = await client.post(path, json=body, headers=upstream_headers)
    except httpx.TimeoutException:
        return Response(content=b"upstream timeout", status_code=504)
    except httpx.RequestError:
        return Response(content=b"upstream connection error", status_code=502)

    if upstream.status_code != 200:
        return sanitize_upstream_error(upstream, extra_headers, provider=provider)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=_response_headers(upstream, extra_headers),
    )
