from __future__ import annotations

from collections.abc import AsyncIterator

from proxy.app.providers.usage import UsageMetrics
from proxy.app.stream_usage import capture_stream_usage


async def _aiter(chunks: list[bytes | str]) -> AsyncIterator[bytes | str]:
    for chunk in chunks:
        yield chunk


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[UsageMetrics] = []

    async def __call__(self, usage: UsageMetrics) -> None:
        self.calls.append(usage)


async def _drain(chunks: list[bytes | str], *, protocol="openai_chat", provider="openai"):
    recorder = _Recorder()
    received = []
    async for chunk in capture_stream_usage(
        _aiter(chunks), protocol=protocol, provider=provider, on_complete=recorder
    ):
        received.append(chunk)
    return received, recorder


# ---------------------------------------------------------------------------
# AC3 — pass-through is unaffected
# ---------------------------------------------------------------------------


async def test_chunks_are_yielded_unchanged():
    chunks = [
        b'data: {"id": "1", "choices": [{"delta": {"content": "hi"}}]}\n\n',
        b'data: {"id": "2", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}\n\n',
        b"data: [DONE]\n\n",
    ]
    received, _ = await _drain(chunks)
    assert received == chunks


async def test_str_chunks_are_yielded_unchanged():
    chunks = [
        'data: {"id": "1", "choices": [{"delta": {"content": "hi"}}]}\n\n',
        'data: {"id": "2", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}\n\n',
        "data: [DONE]\n\n",
    ]
    received, recorder = await _drain(chunks)
    assert received == chunks
    assert recorder.calls == [UsageMetrics(1, 1, 2)]


# ---------------------------------------------------------------------------
# AC1 — usage present in-stream is captured
# ---------------------------------------------------------------------------


async def test_openai_chat_usage_on_final_chunk_is_captured():
    chunks = [
        b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n',
        b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}\n\n',
        b"data: [DONE]\n\n",
    ]
    _, recorder = await _drain(chunks)
    assert recorder.calls == [UsageMetrics(10, 5, 15)]


async def test_anthropic_messages_protocol_accumulates_across_two_events():
    chunks = [
        b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}\n\n',
        b'data: {"type": "content_block_delta", "delta": {"text": "hi"}}\n\n',
        b'data: {"type": "message_delta", "usage": {"output_tokens": 4}}\n\n',
        b"data: [DONE]\n\n",
    ]
    _, recorder = await _drain(chunks, protocol="anthropic_messages", provider="anthropic")
    assert recorder.calls == [UsageMetrics(12, 4, 16)]


async def test_openai_responses_protocol_extracts_nested_usage():
    chunks = [
        b'data: {"type": "response.output_text.delta", "delta": "hi"}\n\n',
        b'data: {"type": "response.completed", "response": {"usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}}\n\n',
        b"data: [DONE]\n\n",
    ]
    _, recorder = await _drain(chunks, protocol="openai_responses", provider="openai")
    assert recorder.calls == [UsageMetrics(7, 3, 10)]


# ---------------------------------------------------------------------------
# AC2 — no usage event in-stream still calls on_complete with zeros
# ---------------------------------------------------------------------------


async def test_no_usage_event_calls_on_complete_with_zero():
    chunks = [
        b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n',
        b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    _, recorder = await _drain(chunks)
    assert recorder.calls == [UsageMetrics.zero()]


async def test_empty_stream_still_calls_on_complete_with_zero():
    _, recorder = await _drain([])
    assert recorder.calls == [UsageMetrics.zero()]


# ---------------------------------------------------------------------------
# AC4 — malformed or missing usage never raises, on_complete still fires
# ---------------------------------------------------------------------------


async def test_malformed_json_does_not_raise():
    chunks = [
        b"data: {not valid json\n\n",
        b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}\n\n',
        b"data: [DONE]\n\n",
    ]
    received, recorder = await _drain(chunks)
    assert received == chunks
    assert recorder.calls == [UsageMetrics(2, 1, 3)]


async def test_non_dict_usage_field_does_not_raise():
    chunks = [
        b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": "not-an-object"}\n\n',
        b"data: [DONE]\n\n",
    ]
    received, recorder = await _drain(chunks)
    assert received == chunks
    assert recorder.calls == [UsageMetrics.zero()]


async def test_non_dict_event_does_not_raise():
    chunks = [
        b"data: [1, 2, 3]\n\n",
        b"data: [DONE]\n\n",
    ]
    received, recorder = await _drain(chunks)
    assert received == chunks
    assert recorder.calls == [UsageMetrics.zero()]


async def test_anthropic_messages_missing_message_start_does_not_raise():
    chunks = [
        b'data: {"type": "message_delta", "usage": {"output_tokens": 4}}\n\n',
        b"data: [DONE]\n\n",
    ]
    _, recorder = await _drain(chunks, protocol="anthropic_messages", provider="anthropic")
    assert recorder.calls == [UsageMetrics(0, 4, 4)]


# ---------------------------------------------------------------------------
# Byte-buffering correctness
# ---------------------------------------------------------------------------


async def test_sse_frame_split_across_multiple_raw_byte_chunks():
    """Raw-byte-passthrough providers deliver chunks that are not aligned to
    SSE frame boundaries; a frame split mid-way must still be parsed once the
    remainder arrives."""
    full = b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8}}\n\n'
    split_point = 20
    chunks = [full[:split_point], full[split_point:], b"data: [DONE]\n\n"]
    received, recorder = await _drain(chunks)
    assert received == chunks
    assert recorder.calls == [UsageMetrics(6, 2, 8)]


async def test_multibyte_utf8_character_split_across_chunk_boundary():
    """A multi-byte UTF-8 character split across a raw chunk boundary must
    not raise or corrupt the usage parse of the frame it belongs to."""
    payload = 'data: {"choices": [{"delta": {"content": "café"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}\n\n'
    encoded = payload.encode("utf-8")
    split_point = encoded.index(b"caf") + 4
    chunks = [encoded[:split_point], encoded[split_point:], b"data: [DONE]\n\n"]
    received, recorder = await _drain(chunks)
    assert received == chunks
    assert recorder.calls == [UsageMetrics(1, 1, 2)]
