from __future__ import annotations

import json

import httpx
from proxy.app.providers.errors import sanitize_upstream_error
from starlette.responses import Response, StreamingResponse

ANTHROPIC_BASE = "https://api.anthropic.com"

_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "stop_sequence": "stop",
}


def make_client(api_key: str) -> httpx.AsyncClient:
    """Create the shared Anthropic client. Call once at lifespan startup."""
    return httpx.AsyncClient(
        base_url=ANTHROPIC_BASE,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
    )


def _translate_request(body: dict) -> dict:
    """Translate OpenAI chat/completions body to Anthropic Messages API body."""
    messages: list[dict] = body.get("messages", [])

    system_parts: list[str] = []
    anthropic_messages: list[dict] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                system_parts.extend(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            continue

        if role == "tool":
            # OpenAI tool result → Anthropic user message with tool_result block
            anthropic_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id", ""),
                            "content": content if isinstance(content, str) else json.dumps(content),
                        }
                    ],
                }
            )
            continue

        if role == "assistant" and msg.get("tool_calls"):
            # Assistant message with tool calls → content blocks
            content_blocks: list[dict] = []
            if content:
                text = content if isinstance(content, str) else json.dumps(content)
                if text:
                    content_blocks.append({"type": "text", "text": text})
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    parsed_args = raw_args
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": parsed_args,
                    }
                )
            anthropic_messages.append({"role": "assistant", "content": content_blocks})
            continue

        # Plain user / assistant message — pass through
        anthropic_messages.append({"role": role, "content": content})

    result: dict = {
        "model": body.get("model", ""),
        "messages": anthropic_messages,
        "max_tokens": body.get("max_tokens", 1024),
        "stream": body.get("stream", False),
    }

    if system_parts:
        result["system"] = "\n\n".join(system_parts)

    for key in ("temperature", "top_p"):
        if key in body:
            result[key] = body[key]

    if "stop" in body:
        stop = body["stop"]
        result["stop_sequences"] = stop if isinstance(stop, list) else [stop]

    if "tools" in body:
        result["tools"] = body["tools"]

    return result


def _translate_response(anthropic_json: dict) -> dict:
    """Translate an Anthropic Messages response to an OpenAI chat.completion envelope."""
    content_blocks: list[dict] = anthropic_json.get("content", [])

    text_parts: list[str] = []
    tool_calls: list[dict] = []

    for block in content_blocks:
        btype = block.get("type", "")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )

    joined_text = "".join(text_parts)
    stop_reason = anthropic_json.get("stop_reason", "end_turn")
    finish_reason = _STOP_REASON_MAP.get(stop_reason, "stop")

    message: dict = {"role": "assistant", "content": joined_text}
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage_raw = anthropic_json.get("usage", {})
    input_tokens = usage_raw.get("input_tokens", 0)
    output_tokens = usage_raw.get("output_tokens", 0)

    return {
        "id": anthropic_json.get("id", ""),
        "object": "chat.completion",
        "model": anthropic_json.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


async def chat_completions(
    client: httpx.AsyncClient,
    body: dict,
    stream: bool,
    extra_headers: dict[str, str],
) -> Response | StreamingResponse:
    """Forward to Anthropic Messages API. Returns a Starlette Response."""
    anthropic_body = _translate_request(body)

    if stream:
        model = body.get("model", "")

        async def _stream_body():
            completion_id = ""
            # Maps content_block index → tool_calls array index for tool_use blocks
            tool_block_index: dict[int, int] = {}
            tool_call_counter = 0
            try:
                async with client.stream("POST", "/v1/messages", json=anthropic_body) as upstream:
                    async for line in upstream.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:") :].strip()
                        if not data_str:
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        etype = event.get("type", "")

                        if etype == "message_start":
                            msg = event.get("message", {})
                            completion_id = msg.get("id", "")
                            continue

                        if etype == "content_block_start":
                            cb = event.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                block_idx = event.get("index", 0)
                                tc_idx = tool_call_counter
                                tool_block_index[block_idx] = tc_idx
                                tool_call_counter += 1
                                chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "tool_calls": [
                                                    {
                                                        "index": tc_idx,
                                                        "id": cb.get("id", ""),
                                                        "type": "function",
                                                        "function": {
                                                            "name": cb.get("name", ""),
                                                            "arguments": "",
                                                        },
                                                    }
                                                ]
                                            },
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"
                            continue

                        if etype == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": delta.get("text", "")},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"
                            elif delta.get("type") == "input_json_delta":
                                block_idx = event.get("index", 0)
                                if block_idx not in tool_block_index:
                                    continue
                                tc_idx = tool_block_index[block_idx]
                                chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "tool_calls": [
                                                    {
                                                        "index": tc_idx,
                                                        "function": {
                                                            "arguments": delta.get("partial_json", ""),
                                                        },
                                                    }
                                                ]
                                            },
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"
                            continue

                        if etype == "message_delta":
                            delta = event.get("delta", {})
                            stop_reason = delta.get("stop_reason", "end_turn")
                            finish_reason = _STOP_REASON_MAP.get(stop_reason, "stop")
                            final_chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": finish_reason,
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(final_chunk)}\n\n"
                            yield "data: [DONE]\n\n"
                            continue
            except httpx.TimeoutException:
                yield 'data: {"error": {"type": "upstream_timeout"}}\n\n'
            except httpx.RequestError:
                yield 'data: {"error": {"type": "upstream_connection_error"}}\n\n'

        return StreamingResponse(
            _stream_body(),
            status_code=200,
            media_type="text/event-stream",
            headers=extra_headers,
        )

    try:
        upstream = await client.post("/v1/messages", json=anthropic_body)
    except httpx.TimeoutException:
        return Response(content=b"upstream timeout", status_code=504)
    except httpx.RequestError:
        return Response(content=b"upstream connection error", status_code=502)

    if upstream.status_code != 200:
        return sanitize_upstream_error(upstream, extra_headers, provider="anthropic")

    envelope = _translate_response(upstream.json())
    return Response(
        content=json.dumps(envelope),
        status_code=200,
        media_type="application/json",
        headers=extra_headers,
    )
