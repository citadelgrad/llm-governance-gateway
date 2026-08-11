from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import cast

import httpx
from proxy.app.protocol_types import JsonObject
from proxy.app.providers.errors import sanitize_upstream_error
from proxy.app.providers.native import forward_native, open_checked_stream
from proxy.app.stream_events import (
    iter_anthropic_messages_canonical_events,
    iter_openai_chat_sse_from_canonical,
)
from starlette.responses import Response, StreamingResponse

ANTHROPIC_BASE = "https://api.anthropic.com"

_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "stop_sequence": "stop",
}


def _int_field(obj: object, key: str) -> int:
    if not isinstance(obj, dict):
        return 0
    value = cast(JsonObject, obj).get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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


async def messages(
    client: httpx.AsyncClient,
    body: JsonObject,
    stream: bool,
    extra_headers: dict[str, str],
    upstream_headers: dict[str, str] | None = None,
) -> Response | StreamingResponse:
    """Forward a validated Anthropic Messages request without Chat translation."""
    return await forward_native(
        client,
        path="/v1/messages",
        body=body,
        stream=stream,
        extra_headers=extra_headers,
        provider="anthropic",
        upstream_headers=upstream_headers,
    )


def _translate_tools(tools: object) -> list[JsonObject]:
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")
    translated: list[JsonObject] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ValueError(f"tool {index} is not an OpenAI function tool")
        tool_object = cast(JsonObject, tool)
        if tool_object.get("type") != "function":
            raise ValueError(f"tool {index} is not an OpenAI function tool")
        function = tool_object.get("function")
        if not isinstance(function, dict):
            raise ValueError(f"tool {index} requires a function definition")
        function_object = cast(JsonObject, function)
        name = function_object.get("name")
        if not isinstance(name, str):
            raise ValueError(f"tool {index} requires a function name")
        input_schema = function_object.get("parameters", {"type": "object"})
        if not isinstance(input_schema, dict):
            raise ValueError(f"tool {index} parameters must be a JSON Schema object")
        translated_tool: JsonObject = {
            "name": name,
            "input_schema": cast(JsonObject, input_schema),
        }
        for key in ("description", "strict"):
            if key in function_object:
                translated_tool[key] = function_object[key]
        translated.append(translated_tool)
    return translated


def _translate_tool_choice(tool_choice: object) -> JsonObject:
    if tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "none":
        return {"type": "none"}
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, dict):
        choice_object = cast(JsonObject, tool_choice)
        function = choice_object.get("function")
        if choice_object.get("type") != "function" or not isinstance(function, dict):
            raise ValueError("Anthropic adapter only supports named function tool_choice")
        name = cast(JsonObject, function).get("name")
        if not isinstance(name, str):
            raise ValueError("named function tool_choice requires a name")
        return {"type": "tool", "name": name}
    raise ValueError("unsupported Anthropic tool_choice")


def _translate_content(
    content: object,
    *,
    location: str,
    allow_images: bool = True,
) -> str | list[JsonObject]:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError(f"{location} content must be a string or content-part list")

    translated: list[JsonObject] = []
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            raise ValueError(f"{location} content part {index} must be an object")
        part_object = cast(JsonObject, part)
        part_type = part_object.get("type")
        if part_type in {"text", "input_text"}:
            text = part_object.get("text")
            if not isinstance(text, str):
                raise ValueError(f"{location} text part {index} requires string text")
            translated.append({"type": "text", "text": text})
            continue
        if part_type == "image_url" and allow_images:
            image_url = part_object.get("image_url")
            if not isinstance(image_url, dict):
                raise ValueError(f"{location} image part {index} requires image_url")
            url = cast(JsonObject, image_url).get("url")
            if not isinstance(url, str):
                raise ValueError(f"{location} image part {index} requires a URL")
            translated.append({"type": "image", "source": {"type": "url", "url": url}})
            continue
        raise ValueError(
            f"{location} content part {index} of type {part_type!r} is unsupported"
        )
    return translated


def _translate_request(body: JsonObject) -> JsonObject:
    """Translate OpenAI chat/completions body to Anthropic Messages API body."""
    messages_value = body.get("messages", [])
    if not isinstance(messages_value, list) or not all(
        isinstance(message, dict) for message in messages_value
    ):
        raise ValueError("messages must be a list of objects")
    messages = cast(list[JsonObject], messages_value)

    system_parts: list[str] = []
    anthropic_messages: list[JsonObject] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role in {"system", "developer"}:
            system_content = _translate_content(
                content,
                location=f"{role} message",
                allow_images=False,
            )
            if isinstance(system_content, str):
                system_parts.append(system_content)
            else:
                for block in system_content:
                    text = block.get("text")
                    if block.get("type") == "text" and isinstance(text, str):
                        system_parts.append(text)
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
                            "content": _translate_content(
                                content,
                                location="tool message",
                                allow_images=False,
                            ),
                        }
                    ],
                }
            )
            continue

        if role == "assistant" and msg.get("tool_calls"):
            # Assistant message with tool calls → content blocks
            content_blocks: list[JsonObject] = []
            if content:
                translated_content = _translate_content(
                    content,
                    location="assistant message",
                )
                if isinstance(translated_content, str):
                    content_blocks.append({"type": "text", "text": translated_content})
                else:
                    content_blocks.extend(translated_content)
            tool_calls = msg["tool_calls"]
            if not isinstance(tool_calls, list):
                raise ValueError("assistant tool_calls must be a list")
            for index, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    raise ValueError(f"assistant tool call {index} must be an object")
                tool_call = cast(JsonObject, tc)
                fn = tool_call.get("function", {})
                if not isinstance(fn, dict):
                    raise ValueError(f"assistant tool call {index} requires a function")
                function_object = cast(JsonObject, fn)
                raw_args = function_object.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"assistant tool call {index} arguments are not valid JSON"
                    ) from exc
                if not isinstance(parsed_args, dict):
                    raise ValueError(f"assistant tool call {index} arguments must be an object")
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call.get("id", ""),
                        "name": function_object.get("name", ""),
                        "input": cast(JsonObject, parsed_args),
                    }
                )
            anthropic_messages.append({"role": "assistant", "content": content_blocks})
            continue

        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported Chat message role {role!r}")
        anthropic_messages.append(
            {
                "role": role,
                "content": _translate_content(content, location=f"{role} message"),
            }
        )

    result: JsonObject = {
        "model": body.get("model", ""),
        "messages": anthropic_messages,
        "max_tokens": body.get("max_tokens") or body.get("max_completion_tokens") or 1024,
        "stream": body.get("stream", False),
    }

    if system_parts:
        result["system"] = "\n\n".join(system_parts)

    for key in (
        "cache_control",
        "container",
        "inference_geo",
        "metadata",
        "output_config",
        "service_tier",
        "temperature",
        "thinking",
        "top_k",
        "top_p",
        "anthropic-user-profile-id",
    ):
        if key in body:
            result[key] = body[key]

    if "stop" in body:
        stop = body["stop"]
        result["stop_sequences"] = stop if isinstance(stop, list) else [stop]

    if "tools" in body:
        result["tools"] = _translate_tools(body["tools"])
    if "tool_choice" in body:
        result["tool_choice"] = _translate_tool_choice(body["tool_choice"])

    return result


def _translate_response(anthropic_json: JsonObject) -> JsonObject:
    """Translate an Anthropic Messages response to an OpenAI chat.completion envelope."""
    raw_content = anthropic_json.get("content", [])
    if not isinstance(raw_content, list):
        raise ValueError("Anthropic response content must be a list")

    text_parts: list[str] = []
    tool_calls: list[JsonObject] = []

    for index, raw_block in enumerate(raw_content):
        if not isinstance(raw_block, dict):
            raise ValueError(f"Anthropic response content block {index} must be an object")
        block = cast(JsonObject, raw_block)
        btype = block.get("type", "")
        if btype == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError(f"Anthropic text block {index} must include string text")
            text_parts.append(text)
        elif btype == "tool_use":
            tool_id = block.get("id")
            name = block.get("name")
            input_value = block.get("input", {})
            if not isinstance(tool_id, str) or not isinstance(name, str):
                raise ValueError(f"Anthropic tool_use block {index} is missing id or name")
            if not isinstance(input_value, dict):
                raise ValueError(f"Anthropic tool_use block {index} input must be an object")
            tool_calls.append(
                {
                    "id": tool_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(input_value),
                    },
                }
            )
        else:
            raise ValueError(
                f"Anthropic response content block {index} type {btype!r} cannot be translated"
            )

    joined_text = "".join(text_parts)
    raw_stop_reason = anthropic_json.get("stop_reason", "end_turn")
    if not isinstance(raw_stop_reason, str):
        raise ValueError("Anthropic response stop_reason must be a string")
    stop_reason = raw_stop_reason
    finish_reason = _STOP_REASON_MAP.get(stop_reason, "stop")

    message: JsonObject = {"role": "assistant", "content": joined_text}
    if tool_calls:
        message["tool_calls"] = tool_calls

    raw_usage = anthropic_json.get("usage", {})
    if not isinstance(raw_usage, dict):
        raise ValueError("Anthropic response usage must be an object")
    usage_raw = cast(JsonObject, raw_usage)
    raw_input_tokens = usage_raw.get("input_tokens", 0)
    raw_output_tokens = usage_raw.get("output_tokens", 0)
    if not isinstance(raw_input_tokens, int) or not isinstance(raw_output_tokens, int):
        raise ValueError("Anthropic response usage token counts must be integers")
    input_tokens = raw_input_tokens
    output_tokens = raw_output_tokens

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
    body: JsonObject,
    stream: bool,
    extra_headers: dict[str, str],
) -> Response | StreamingResponse:
    """Forward to Anthropic Messages API. Returns a Starlette Response."""
    try:
        anthropic_body = _translate_request(body)
    except ValueError as exc:
        return Response(
            content=json.dumps(
                {"error": {"type": "unsupported_chat_translation", "message": str(exc)}}
            ),
            status_code=422,
            media_type="application/json",
            headers=extra_headers,
        )

    if stream:
        model = body.get("model", "")
        upstream, error_response = await open_checked_stream(
            client,
            "POST",
            "/v1/messages",
            body=anthropic_body,
            extra_headers=extra_headers,
            provider="anthropic",
        )
        if error_response is not None:
            return error_response
        assert upstream is not None

        @asynccontextmanager
        async def _opened_stream():
            try:
                yield upstream
            finally:
                await upstream.aclose()

        async def _stream_body():
            try:
                async with _opened_stream() as checked_upstream:
                    canonical_events = iter_anthropic_messages_canonical_events(
                        checked_upstream.aiter_lines()
                    )
                    async for chunk in iter_openai_chat_sse_from_canonical(
                        canonical_events,
                        model=str(model),
                    ):
                        yield chunk
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
