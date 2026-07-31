from __future__ import annotations

import asyncio

import pytest
from mcpproxy.app.transport import (
    ToolResponseTimeoutError,
    ToolResponseTooLargeError,
    buffer_tool_response,
)


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


async def _stalling_chunks(*parts: bytes, stall_after: int, stall_seconds: float):
    for i, part in enumerate(parts):
        if i == stall_after:
            await asyncio.sleep(stall_seconds)
        yield part


async def test_small_fast_response_passes_through_unaffected():
    """AC1/AC6: single JSON-RPC-style chunk, well under cap, returns intact."""
    result = await buffer_tool_response(_chunks(b'{"result": "ok"}'), max_bytes=1024)
    assert result == b'{"result": "ok"}'


async def test_multi_chunk_sse_style_response_concatenates_before_returning():
    """AC1: SSE-style multi-chunk response is fully buffered before returning."""
    result = await buffer_tool_response(_chunks(b"event: a\n", b"data: b\n", b"\n"), max_bytes=1024)
    assert result == b"event: a\ndata: b\n\n"


async def test_response_within_bounds_completes_normally():
    """AC3: completes within cap and timeout, so it is returned for the next step."""
    result = await buffer_tool_response(
        _chunks(b"a" * 10, b"b" * 10), max_bytes=1024, timeout_seconds=1.0
    )
    assert result == b"a" * 10 + b"b" * 10


async def test_cumulative_overflow_aborts_the_instant_cap_is_crossed():
    """AC2/AC5: the crossing chunk triggers immediate abort - later chunks never consumed."""
    consumed = []

    async def chunks():
        consumed.append(1)
        yield b"a" * 6
        consumed.append(2)
        yield b"b" * 6  # cumulative 12 > max_bytes=10, should abort here
        consumed.append(3)
        yield b"c" * 6  # must never be reached

    with pytest.raises(ToolResponseTooLargeError):
        await buffer_tool_response(chunks(), max_bytes=10, timeout_seconds=1.0)

    assert consumed == [1, 2]


async def test_single_chunk_exceeding_cap_aborts_immediately():
    """AC2/AC5: a single oversized chunk is rejected without waiting for more data."""
    with pytest.raises(ToolResponseTooLargeError):
        await buffer_tool_response(_chunks(b"x" * 20), max_bytes=10, timeout_seconds=1.0)


async def test_stalled_response_times_out_and_is_treated_as_failure():
    """AC4: incomplete after the wall-clock cap is aborted as a failure."""
    with pytest.raises(ToolResponseTimeoutError):
        await buffer_tool_response(
            _stalling_chunks(b"a", b"b", stall_after=1, stall_seconds=0.2),
            max_bytes=1024,
            timeout_seconds=0.05,
        )


async def test_defaults_match_documented_1mib_and_10s_caps():
    from mcpproxy.app.transport import MAX_TOOL_RESPONSE_BYTES, TOOL_RESPONSE_TIMEOUT_SECONDS

    assert MAX_TOOL_RESPONSE_BYTES == 1 * 1024 * 1024
    assert TOOL_RESPONSE_TIMEOUT_SECONDS == 10.0
