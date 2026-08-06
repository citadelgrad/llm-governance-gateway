from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from proxy.app.protocol_types import JsonObject, WireProtocol
from proxy.app.providers.usage import UsageMetrics, extract_usage

logger = logging.getLogger(__name__)

StreamChunk = bytes | str | memoryview
OnStreamComplete = Callable[[UsageMetrics], Awaitable[None]]


def _token_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass
class _UsageState:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_usage(self) -> UsageMetrics:
        return UsageMetrics(
            self.prompt_tokens, self.completion_tokens, self.prompt_tokens + self.completion_tokens
        )


def _apply_event(protocol: WireProtocol, provider: str, event: JsonObject, state: _UsageState) -> None:
    """Update `state` from one parsed SSE data event, if it carries usage."""
    if protocol == "anthropic_messages":
        # Native Anthropic streams split usage across two events: input_tokens
        # on message_start, output_tokens on message_delta.
        etype = event.get("type")
        if etype == "message_start":
            msg = event.get("message")
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if isinstance(usage, dict):
                state.prompt_tokens = _token_count(usage.get("input_tokens"))
        elif etype == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, dict):
                state.completion_tokens = _token_count(usage.get("output_tokens"))
        return

    if protocol == "openai_responses":
        response = event.get("response")
        usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, dict):
            extracted = extract_usage("openai", {"usage": usage})
            state.prompt_tokens = extracted.prompt_tokens
            state.completion_tokens = extracted.completion_tokens
        return

    # openai_chat covers native OpenAI chat completions and every
    # compat-translated adapter (anthropic, gemini, mock, ollama, generic),
    # all of which serialize their final usage event in Chat shape.
    usage = event.get("usage")
    if isinstance(usage, dict):
        extracted = extract_usage(provider, event)
        state.prompt_tokens = extracted.prompt_tokens
        state.completion_tokens = extracted.completion_tokens


async def capture_stream_usage(
    body_iterator: AsyncIterable[StreamChunk],
    *,
    protocol: WireProtocol,
    provider: str,
    on_complete: OnStreamComplete,
) -> AsyncIterator[StreamChunk]:
    """Pass a streaming response body through unchanged while watching for the
    provider's usage-bearing SSE event. Calls `on_complete` exactly once, with
    the last usage seen (or UsageMetrics.zero() if none was found). A
    malformed or missing usage event is swallowed and never propagates to the
    client stream (AC4).
    """
    state = _UsageState()
    buffer = b""
    try:
        async for chunk in body_iterator:
            yield chunk
            try:
                if isinstance(chunk, str):
                    buffer += chunk.encode("utf-8")
                else:
                    buffer += bytes(chunk)
                while b"\n\n" in buffer:
                    event_bytes, buffer = buffer.split(b"\n\n", 1)
                    event_text = event_bytes.decode("utf-8", errors="replace")
                    for line in event_text.splitlines():
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:") :].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            _apply_event(protocol, provider, event, state)
            except Exception:
                logger.exception("Failed to parse usage from stream chunk")
    finally:
        try:
            await on_complete(state.to_usage())
        except Exception:
            logger.exception("Failed to record streaming usage")
