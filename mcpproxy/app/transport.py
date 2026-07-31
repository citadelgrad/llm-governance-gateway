from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

# Matches proxy/app/middleware.py's MAX_BODY_SIZE convention.
MAX_TOOL_RESPONSE_BYTES = 1 * 1024 * 1024
TOOL_RESPONSE_TIMEOUT_SECONDS = 10.0


class ToolResponseTooLargeError(Exception):
    """Raised when a buffered tool response exceeds the size cap."""


class ToolResponseTimeoutError(Exception):
    """Raised when buffering a tool response exceeds the wall-clock cap."""


async def buffer_tool_response(
    chunks: AsyncIterator[bytes],
    *,
    max_bytes: int = MAX_TOOL_RESPONSE_BYTES,
    timeout_seconds: float = TOOL_RESPONSE_TIMEOUT_SECONDS,
) -> bytes:
    """Fully buffer an async byte-chunk iterator, fail-closed on cap or timeout breach.

    Presidio has no streaming API, so tool responses are always fully buffered
    before anything else happens to them. JSON-RPC (one chunk) and SSE (many
    chunks) both reduce to the same async byte-chunk iterator, so no separate
    per-transport code path is needed.
    """

    async def _consume() -> bytes:
        buffer = bytearray()
        async for chunk in chunks:
            buffer += chunk
            if len(buffer) > max_bytes:
                raise ToolResponseTooLargeError(
                    f"tool response exceeded {max_bytes} byte cap"
                )
        return bytes(buffer)

    try:
        return await asyncio.wait_for(_consume(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise ToolResponseTimeoutError(
            f"tool response not received within {timeout_seconds}s"
        ) from exc
