"""Generic OpenAI-compatible pass-through adapter for unknown providers with a base_url."""

import asyncio
import ipaddress
import json
import logging
from urllib.parse import urlparse

import httpx
from proxy.app.providers.errors import sanitize_upstream_error
from starlette.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)


class InvalidBaseURLError(ValueError):
    """Raised when a configured base_url fails the safety checks."""


def _validate_base_url(base_url: str) -> None:
    """Reject schemes other than https and hostnames that resolve to private/link-local IPs.

    Plain http is permitted only for localhost (Ollama-style local dev).
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise InvalidBaseURLError(f"unsupported scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise InvalidBaseURLError("missing host in base_url")

    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise InvalidBaseURLError("http:// is only allowed for localhost")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostname (not a literal IP) — we don't pre-resolve; rely on outbound
        # firewall / network policy. Block obviously dangerous literals only.
        return

    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise InvalidBaseURLError(f"forbidden ip range: {host}")
    if ip.is_private and host not in ("127.0.0.1", "::1"):
        raise InvalidBaseURLError(f"forbidden private ip: {host}")


def _sanitise_api_key(api_key: str) -> str:
    """Strip CR/LF to defend against header-injection from misconfigured models.yaml."""
    return api_key.replace("\r", "").replace("\n", "").strip()


def _extract_origin(base_url: str) -> str:
    """Return scheme + netloc (host + optional port, no path) as the pool key."""
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


# ---------------------------------------------------------------------------
# Per-base_url client pool
# ---------------------------------------------------------------------------

_CLIENT_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=100)
_CLIENT_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)

_MAX_POOL_SIZE = 50  # base_url comes from models.yaml; cardinality should be well below this

_pool: dict[str, httpx.AsyncClient] = {}
_pool_lock: asyncio.Lock | None = None  # lazy-init inside running event loop


async def get_pooled_client(origin: str) -> httpx.AsyncClient:
    """Return a cached AsyncClient for *origin*, creating one on first access."""
    global _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    async with _pool_lock:
        if origin not in _pool:
            if len(_pool) >= _MAX_POOL_SIZE:
                raise RuntimeError(
                    f"generic_pool size limit ({_MAX_POOL_SIZE}) reached; "
                    f"cannot create client for {origin}"
                )
            _pool[origin] = httpx.AsyncClient(
                base_url=origin,
                limits=_CLIENT_LIMITS,
                timeout=_CLIENT_TIMEOUT,
            )
        return _pool[origin]


async def close_all_clients() -> None:
    """Close every pooled client. Call once at application shutdown."""
    global _pool_lock
    if _pool_lock is None:
        return
    async with _pool_lock:
        for client in _pool.values():
            await client.aclose()
        _pool.clear()


async def chat_completions(
    body: dict,
    stream: bool,
    extra_headers: dict[str, str],
    *,
    base_url: str,
    api_key: str = "",
) -> Response | StreamingResponse:
    """Forward to an OpenAI-compatible endpoint. Returns a Starlette Response."""
    try:
        _validate_base_url(base_url)
    except InvalidBaseURLError as exc:
        return Response(
            content=json.dumps(
                {"error": {"type": "invalid_provider_config", "message": str(exc)}}
            ).encode(),
            status_code=502,
            media_type="application/json",
            headers=extra_headers,
        )

    origin = _extract_origin(base_url)
    try:
        client = await get_pooled_client(origin)
    except RuntimeError as exc:
        logger.error("generic_pool exhausted: %s", exc)
        return Response(
            content=json.dumps(
                {"error": {"type": "api_error", "message": "Service temporarily unavailable"}}
            ).encode(),
            status_code=503,
            media_type="application/json",
            headers=extra_headers,
        )
    # Use an absolute URL so it works even if the client has a base_url set.
    url = f"{base_url.rstrip('/')}/chat/completions"
    safe_key = _sanitise_api_key(api_key)
    auth_headers = {"Authorization": f"Bearer {safe_key}"} if safe_key else {}

    if stream:
        async def _stream_body():
            try:
                async with client.stream("POST", url, json=body, headers=auth_headers) as upstream:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
            except httpx.TimeoutException:
                yield b'data: {"error": {"type": "upstream_timeout"}}\n\n'
            except httpx.RequestError:
                yield b'data: {"error": {"type": "upstream_connection_error"}}\n\n'

        return StreamingResponse(
            _stream_body(),
            status_code=200,
            media_type="text/event-stream",
            headers=extra_headers,
        )

    try:
        upstream = await client.post(url, json=body, headers=auth_headers)
    except httpx.TimeoutException:
        return Response(content=b"upstream timeout", status_code=504)
    except httpx.RequestError:
        return Response(content=b"upstream connection error", status_code=502)

    if upstream.status_code != 200:
        return sanitize_upstream_error(upstream, extra_headers, provider="generic")

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=extra_headers,
    )
