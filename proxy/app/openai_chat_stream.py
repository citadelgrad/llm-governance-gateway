"""OpenAI Chat Completions SSE stream decoding."""
from __future__ import annotations

import json
from codecs import getincrementaldecoder
from collections.abc import AsyncIterator

from proxy.app.protocol_types import JsonObject, OpenAIChatCompletionChunk
from pydantic import ValidationError

type ChatStreamError = JsonObject
type DecodedChatStreamEvent = OpenAIChatCompletionChunk | ChatStreamError


def _stream_error(message: str) -> JsonObject:
    return {"error": {"type": "invalid_upstream_stream", "message": message}}


async def _iter_sse_json(body_iterator: AsyncIterator[str | bytes]) -> AsyncIterator[JsonObject]:
    """Parse JSON payloads from raw OpenAI Chat SSE `data:` lines."""
    buffer = ""
    decoder = getincrementaldecoder("utf-8")(errors="strict")

    async for raw in body_iterator:
        try:
            if isinstance(raw, bytes):
                buffer += decoder.decode(raw, final=False)
            else:
                pending, _ = decoder.getstate()
                if pending:
                    yield _stream_error("Provider stream changed chunk type during UTF-8 sequence")
                    return
                buffer += raw
        except UnicodeDecodeError:
            yield _stream_error("Provider stream contained invalid UTF-8")
            return
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                payload = json.loads(data_str)
            except json.JSONDecodeError:
                yield _stream_error("Provider stream contained malformed JSON")
                return
            if not isinstance(payload, dict):
                yield _stream_error("Provider stream event was not a JSON object")
                return
            yield payload

    try:
        buffer += decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        yield _stream_error("Provider stream ended with incomplete UTF-8")
        return

    # Upstream may close without a trailing newline after the last data: line.
    line = buffer.rstrip("\r")
    if line.startswith("data:"):
        data_str = line[len("data:"):].strip()
        if data_str and data_str != "[DONE]":
            try:
                payload = json.loads(data_str)
            except json.JSONDecodeError:
                yield _stream_error("Provider stream ended with malformed JSON")
                return
            if not isinstance(payload, dict):
                yield _stream_error("Provider stream event was not a JSON object")
                return
            yield payload


async def iter_openai_chat_events(
    body_iterator: AsyncIterator[str | bytes],
) -> AsyncIterator[DecodedChatStreamEvent]:
    """Decode Chat SSE into strict chunks or one sanitized terminal error."""
    async for event in _iter_sse_json(body_iterator):
        if "error" in event and "choices" not in event:
            yield event
            return
        try:
            yield OpenAIChatCompletionChunk.model_validate(event)
        except ValidationError:
            yield _stream_error("Provider stream chunk did not match the Chat protocol")
            return
