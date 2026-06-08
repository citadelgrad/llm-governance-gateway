"""Anthropic Messages API compatibility types and translation helpers for Claude Code support."""
from __future__ import annotations

import json

from pydantic import BaseModel


class AnthropicMessage(BaseModel):
    role: str
    content: str | list[dict]


class AnthropicMessagesRequest(BaseModel):
    model: str
    messages: list[AnthropicMessage]
    system: str | None = None
    max_tokens: int = 1024
    temperature: float | None = None
    stream: bool = False
    top_p: float | None = None
    stop_sequences: list[str] | None = None


class CountTokensRequest(BaseModel):
    model: str
    messages: list[AnthropicMessage]
    system: str | None = None


def _content_str(content: str | list[dict]) -> str:
    if isinstance(content, str):
        return content
    return " ".join(
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def messages_to_chat_body(req: AnthropicMessagesRequest) -> dict:
    """Translate Anthropic Messages request to internal chat completion body."""
    msgs: list[dict] = []
    if req.system:
        msgs.append({"role": "system", "content": req.system})
    for msg in req.messages:
        msgs.append({"role": msg.role, "content": _content_str(msg.content)})

    body: dict = {
        "model": req.model,
        "messages": msgs,
        "max_tokens": req.max_tokens,
        "stream": req.stream,
    }
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.top_p is not None:
        body["top_p"] = req.top_p
    if req.stop_sequences:
        body["stop"] = req.stop_sequences
    return body


_FINISH_TO_STOP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}


def chat_response_to_anthropic(chat_json: dict, model: str) -> dict:
    """Translate OpenAI chat completion JSON to Anthropic Messages API response shape."""
    choices = chat_json.get("choices") or [{}]
    choice = choices[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")
    stop_reason = _FINISH_TO_STOP.get(finish_reason, "end_turn")
    usage = chat_json.get("usage", {})
    return {
        "id": chat_json.get("id", "msg_compat"),
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": message.get("content", "")}],
        "model": chat_json.get("model", model),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def openai_sse_to_anthropic_sse(body_iterator, model: str):
    """Translate OpenAI SSE stream chunks to Anthropic Messages SSE events."""
    _start = {
        "type": "message_start",
        "message": {
            "id": "msg_compat", "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    yield f"event: message_start\ndata: {json.dumps(_start)}\n\n"
    _cb_start = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    yield f"event: content_block_start\ndata: {json.dumps(_cb_start)}\n\n"

    async for raw in body_iterator:
        chunk = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        for line in chunk.splitlines():
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            finish_reason = choices[0].get("finish_reason")

            if delta.get("content"):
                _delta = {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": delta["content"]},
                }
                yield f"event: content_block_delta\ndata: {json.dumps(_delta)}\n\n"

            if finish_reason:
                stop_reason = _FINISH_TO_STOP.get(finish_reason, "end_turn")
                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                _msg_delta = {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                }
                yield f"event: message_delta\ndata: {json.dumps(_msg_delta)}\n\n"
                yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"


def count_tokens_approximate(messages: list[AnthropicMessage], system: str | None = None) -> int:
    """Deterministic token approximation (~4 chars per token, plus per-message overhead)."""
    total_chars = len(system) if system else 0
    for msg in messages:
        total_chars += len(_content_str(msg.content))
    return max(1, (total_chars + 3) // 4 + len(messages) * 5)
