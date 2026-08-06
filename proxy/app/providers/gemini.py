from __future__ import annotations

import json
import secrets
from typing import cast

import httpx
from proxy.app.protocol_types import JsonObject
from proxy.app.provider_capabilities import GEMINI_CHAT_TRANSLATION_FIELDS
from proxy.app.providers.errors import sanitize_upstream_error
from proxy.app.providers.native import open_checked_stream
from starlette.responses import Response, StreamingResponse

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

_FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
}

class GeminiTranslationError(ValueError):
    """Chat semantics cannot be represented by Gemini without loss."""


def _int_field(obj: object, key: str) -> int:
    if not isinstance(obj, dict):
        return 0
    value = cast(JsonObject, obj).get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text_content(content: object, *, location: str) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        raise GeminiTranslationError(f"{location} content must be text")
    text: list[str] = []
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            raise GeminiTranslationError(
                f"{location} content part {index} is not supported by the Gemini adapter"
            )
        part_object = cast(dict[str, object], part)
        if part_object.get("type") not in {"text", "input_text"}:
            raise GeminiTranslationError(
                f"{location} content part {index} is not supported by the Gemini adapter"
            )
        value = part_object.get("text")
        if not isinstance(value, str):
            raise GeminiTranslationError(f"{location} content part {index} requires string text")
        text.append(value)
    return "".join(text)


def make_client(api_key: str) -> httpx.AsyncClient:
    """Create the shared Gemini client. Auth uses the x-goog-api-key header
    rather than ?key= so the key never appears in URL access logs."""
    return httpx.AsyncClient(
        base_url=GEMINI_BASE,
        headers={"x-goog-api-key": api_key, "content-type": "application/json"},
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
    )


def _translate_request(body: JsonObject) -> tuple[str, JsonObject]:
    """Translate Chat to Gemini or fail before losing request semantics."""
    unsupported = sorted(key for key in body if key not in GEMINI_CHAT_TRANSLATION_FIELDS)
    if unsupported:
        raise GeminiTranslationError(
            "Gemini adapter does not support Chat fields: " + ", ".join(unsupported)
        )

    model_value = body.get("model", "gemini-3.1-flash-lite")
    if not isinstance(model_value, str):
        raise GeminiTranslationError("model must be a string")
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        raise GeminiTranslationError("messages must be a list")

    system_parts: list[str] = []
    contents: list[JsonObject] = []
    tool_names_by_call_id: dict[str, str] = {}

    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise GeminiTranslationError(f"message {message_index} must be an object")
        role = message.get("role")
        if not isinstance(role, str):
            raise GeminiTranslationError(f"message {message_index} requires a role")
        text = _text_content(message.get("content"), location=f"message {message_index}")

        if role in {"system", "developer"}:
            system_parts.append(text)
        elif role == "assistant":
            parts: list[JsonObject] = []
            if text:
                parts.append({"text": text})
            tool_calls = message.get("tool_calls", [])
            if not isinstance(tool_calls, list):
                raise GeminiTranslationError(f"message {message_index} tool_calls must be a list")
            for call_index, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, dict) or tool_call.get("type", "function") != "function":
                    raise GeminiTranslationError(
                        f"message {message_index} tool call {call_index} must be a function call"
                    )
                function = tool_call.get("function")
                if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                    raise GeminiTranslationError(
                        f"message {message_index} tool call {call_index} requires a function name"
                    )
                raw_arguments = function.get("arguments", "{}")
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except json.JSONDecodeError as exc:
                    raise GeminiTranslationError(
                        f"message {message_index} tool call {call_index} arguments are not valid JSON"
                    ) from exc
                if not isinstance(arguments, dict):
                    raise GeminiTranslationError(
                        f"message {message_index} tool call {call_index} arguments must be an object"
                    )
                call_id = tool_call.get("id")
                name = function["name"]
                function_call: JsonObject = {"name": name, "args": arguments}
                if isinstance(call_id, str):
                    function_call["id"] = call_id
                    tool_names_by_call_id[call_id] = name
                parts.append({"functionCall": function_call})
            contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in tool_names_by_call_id:
                raise GeminiTranslationError(
                    f"message {message_index} tool_call_id does not reference a prior call"
                )
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": call_id,
                                "name": tool_names_by_call_id[call_id],
                                "response": {"output": text},
                            }
                        }
                    ],
                }
            )
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": text}]})
        else:
            raise GeminiTranslationError(f"message {message_index} has unsupported role {role!r}")

    gemini_body: JsonObject = {"contents": contents}
    if system_parts:
        gemini_body["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}

    generation_config: JsonObject = {}
    if "temperature" in body:
        generation_config["temperature"] = body["temperature"]
    if "top_p" in body:
        generation_config["topP"] = body["top_p"]
    if "max_completion_tokens" in body or "max_tokens" in body:
        generation_config["maxOutputTokens"] = body.get(
            "max_completion_tokens", body.get("max_tokens")
        )
    if "stop" in body:
        stop = body["stop"]
        generation_config["stopSequences"] = [stop] if isinstance(stop, str) else stop
    if generation_config:
        gemini_body["generationConfig"] = generation_config

    tools = body.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise GeminiTranslationError("tools must be a list")
        declarations: list[JsonObject] = []
        for index, tool in enumerate(tools):
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise GeminiTranslationError(f"tool {index} is not a function tool")
            function = tool.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise GeminiTranslationError(f"tool {index} requires a function definition")
            declaration: JsonObject = {"name": function["name"]}
            if "description" in function:
                declaration["description"] = function["description"]
            if "parameters" in function:
                declaration["parameters"] = function["parameters"]
            declarations.append(declaration)
        gemini_body["tools"] = [{"functionDeclarations": declarations}]

    tool_choice = body.get("tool_choice")
    if tool_choice is not None:
        config: JsonObject
        if tool_choice == "auto":
            config = {"mode": "AUTO"}
        elif tool_choice == "none":
            config = {"mode": "NONE"}
        elif tool_choice == "required":
            config = {"mode": "ANY"}
        elif isinstance(tool_choice, dict):
            function = tool_choice.get("function")
            if tool_choice.get("type") != "function" or not isinstance(function, dict):
                raise GeminiTranslationError("Gemini adapter only supports named function tool_choice")
            name = function.get("name")
            if not isinstance(name, str):
                raise GeminiTranslationError("named function tool_choice requires a name")
            config = {"mode": "ANY", "allowedFunctionNames": [name]}
        else:
            raise GeminiTranslationError("unsupported Gemini tool_choice")
        gemini_body["toolConfig"] = {"functionCallingConfig": config}

    return model_value, gemini_body


def _to_openai_envelope(gemini_json: JsonObject, model: str) -> JsonObject:
    """Convert a Gemini generateContent response to an OpenAI chat.completion envelope."""
    raw_candidates = gemini_json.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise GeminiTranslationError("Gemini candidates must be a list")
    raw_usage_meta = gemini_json.get("usageMetadata", {})
    if not isinstance(raw_usage_meta, dict):
        raise GeminiTranslationError("Gemini usageMetadata must be an object")
    usage_meta = cast(JsonObject, raw_usage_meta)

    text_parts: list[str] = []
    tool_calls: list[JsonObject] = []
    finish_reason = "stop"

    if raw_candidates:
        raw_candidate = raw_candidates[0]
        if not isinstance(raw_candidate, dict):
            raise GeminiTranslationError("Gemini candidate must be an object")
        candidate = cast(JsonObject, raw_candidate)
        raw_content = candidate.get("content", {})
        if not isinstance(raw_content, dict):
            raise GeminiTranslationError("Gemini candidate content must be an object")
        content_obj = cast(JsonObject, raw_content)
        raw_parts = content_obj.get("parts", [])
        if not isinstance(raw_parts, list):
            raise GeminiTranslationError("Gemini candidate parts must be a list")
        for index, raw_part in enumerate(raw_parts):
            if not isinstance(raw_part, dict):
                raise GeminiTranslationError(f"Gemini candidate part {index} must be an object")
            part = cast(JsonObject, raw_part)
            if "text" in part:
                text = part["text"]
                if not isinstance(text, str):
                    raise GeminiTranslationError(
                        f"Gemini candidate part {index} text must be a string"
                    )
                text_parts.append(text)
            raw_function_call = part.get("functionCall")
            if isinstance(raw_function_call, dict):
                function_call = cast(JsonObject, raw_function_call)
                name = function_call.get("name")
                if not isinstance(name, str) or not name:
                    raise GeminiTranslationError(
                        f"Gemini functionCall in part {index} must include a name"
                    )
                tool_calls.append(
                    {
                        "id": function_call.get(
                            "id", f"call_gemini_{secrets.token_hex(8)}"
                        ),
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(function_call.get("args", {})),
                        },
                    }
                )
        gemini_finish = candidate.get("finishReason", "STOP")
        if not isinstance(gemini_finish, str):
            raise GeminiTranslationError("Gemini finishReason must be a string")
        if gemini_finish not in _FINISH_REASON_MAP:
            raise GeminiTranslationError(f"Gemini generation failed: {gemini_finish}")
        finish_reason = _FINISH_REASON_MAP[gemini_finish]

    message: JsonObject = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"

    return {
        "id": f"chatcmpl-gemini-{secrets.token_hex(8)}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage_meta.get("promptTokenCount", 0),
            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
            "total_tokens": usage_meta.get("totalTokenCount", 0),
        },
    }


async def chat_completions(
    client: httpx.AsyncClient,
    body: JsonObject,
    stream: bool,
    extra_headers: dict[str, str],
) -> Response | StreamingResponse:
    """Forward to Gemini and return a Starlette Response in OpenAI shape."""
    try:
        model, gemini_body = _translate_request(body)
    except GeminiTranslationError as exc:
        return Response(
            content=json.dumps(
                {"error": {"type": "unsupported_chat_translation", "message": str(exc)}}
            ),
            status_code=422,
            media_type="application/json",
            headers=extra_headers,
        )

    if stream:
        url = f"/models/{model}:streamGenerateContent"
        upstream, error_response = await open_checked_stream(
            client,
            "POST",
            url,
            body=gemini_body,
            extra_headers=extra_headers,
            provider="gemini",
            params={"alt": "sse"},
        )
        if error_response is not None:
            return error_response
        assert upstream is not None

        async def _stream_body():
            completion_id = f"chatcmpl-gemini-{secrets.token_hex(8)}"
            final_finish_reason = "stop"
            usage_meta: JsonObject = {}
            try:
                async for line in upstream.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        raw = line[len("data:") :].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk_json = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        raw_usage_meta = chunk_json.get("usageMetadata")
                        if isinstance(raw_usage_meta, dict):
                            usage_meta = cast(JsonObject, raw_usage_meta)

                        candidates = chunk_json.get("candidates", [])
                        if not candidates:
                            continue
                        candidate = candidates[0]
                        parts = candidate.get("content", {}).get("parts", [])
                        text = "".join(p.get("text", "") for p in parts)
                        tool_call_deltas: list[JsonObject] = []
                        for index, part in enumerate(parts):
                            function_call = part.get("functionCall")
                            if not isinstance(function_call, dict):
                                continue
                            tool_call_deltas.append(
                                {
                                    "index": index,
                                    "id": function_call.get(
                                        "id", f"call_gemini_{secrets.token_hex(8)}"
                                    ),
                                    "type": "function",
                                    "function": {
                                        "name": function_call.get("name", ""),
                                        "arguments": json.dumps(function_call.get("args", {})),
                                    },
                                }
                            )
                        if (gemini_finish := candidate.get("finishReason")):
                            if gemini_finish not in _FINISH_REASON_MAP:
                                error_chunk = {
                                    "error": {
                                        "type": "provider_finish_reason",
                                        "message": f"Gemini generation failed: {gemini_finish}",
                                    }
                                }
                                yield f"data: {json.dumps(error_chunk)}\n\n"
                                return
                            final_finish_reason = _FINISH_REASON_MAP[gemini_finish]

                        delta: JsonObject = {"content": text}
                        if tool_call_deltas:
                            delta["tool_calls"] = tool_call_deltas
                            final_finish_reason = "tool_calls"

                        oai_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": delta,
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(oai_chunk)}\n\n"
            except httpx.TimeoutException:
                yield 'data: {"error": {"type": "upstream_timeout"}}\n\n'
                return
            except httpx.RequestError:
                yield 'data: {"error": {"type": "upstream_connection_error"}}\n\n'
                return
            finally:
                await upstream.aclose()

            prompt_tokens = _int_field(usage_meta, "promptTokenCount")
            completion_tokens = _int_field(usage_meta, "candidatesTokenCount")
            final_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": final_finish_reason}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
            yield f"data: {json.dumps(final_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _stream_body(),
            status_code=200,
            media_type="text/event-stream",
            headers=extra_headers,
        )

    url = f"/models/{model}:generateContent"
    try:
        upstream = await client.post(url, json=gemini_body)
    except httpx.TimeoutException:
        return Response(content=b"upstream timeout", status_code=504)
    except httpx.RequestError:
        return Response(content=b"upstream connection error", status_code=502)

    if upstream.status_code != 200:
        return sanitize_upstream_error(upstream, extra_headers, provider="gemini")

    upstream_json = upstream.json()
    if not isinstance(upstream_json, dict):
        return Response(
            content=json.dumps(
                {
                    "error": {
                        "type": "provider_response_error",
                        "message": "Gemini returned a non-object response",
                    }
                }
            ),
            status_code=502,
            media_type="application/json",
            headers=extra_headers,
        )
    try:
        envelope = _to_openai_envelope(cast(JsonObject, upstream_json), model)
    except GeminiTranslationError as exc:
        return Response(
            content=json.dumps(
                {"error": {"type": "provider_response_error", "message": str(exc)}}
            ),
            status_code=502,
            media_type="application/json",
            headers=extra_headers,
        )
    return Response(
        content=json.dumps(envelope),
        status_code=200,
        media_type="application/json",
        headers=extra_headers,
    )
