from __future__ import annotations

import json
import secrets
from dataclasses import replace
from typing import cast

import httpx
from proxy.app.protocol_types import JsonObject
from proxy.app.provider_capabilities import GEMINI_CHAT_TRANSLATION_FIELDS
from proxy.app.providers._gemini_common import (
    DEVELOPER_API_DIALECT,
    FINISH_REASON_MAP,
    GeminiTranslationError,
    extract_block_reason,
    raise_if_prompt_blocked,
    translate_candidate_to_openai_choice,
    translate_chat_request,
    translate_usage_metadata,
)
from proxy.app.providers.errors import sanitize_upstream_error
from proxy.app.providers.native import open_checked_stream
from starlette.responses import Response, StreamingResponse

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# DEVELOPER_API_DIALECT.extra_finish_reasons is real (see _gemini_common.py)
# but not yet wired into this adapter's response path: this adapter has
# always raised on any finishReason outside FINISH_REASON_MAP, and the
# streaming path below still does. Using a copy with no extras here keeps
# that behavior — accepting the dialect's extra finish reasons is deferred
# until a caller (e.g. the Vertex adapter) actually needs it.
_RESPONSE_DIALECT = replace(DEVELOPER_API_DIALECT, extra_finish_reasons=frozenset())


def _int_field(obj: object, key: str) -> int:
    if not isinstance(obj, dict):
        return 0
    value = cast(JsonObject, obj).get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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
    return translate_chat_request(
        body,
        allowed_fields=GEMINI_CHAT_TRANSLATION_FIELDS,
        default_model="gemini-3.1-flash-lite",
    )


def _to_openai_envelope(gemini_json: JsonObject, model: str) -> JsonObject:
    """Convert a Gemini generateContent response to an OpenAI chat.completion envelope."""
    raw_candidates = gemini_json.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise GeminiTranslationError("Gemini candidates must be a list")
    raw_usage_meta = gemini_json.get("usageMetadata", {})
    if not isinstance(raw_usage_meta, dict):
        raise GeminiTranslationError("Gemini usageMetadata must be an object")
    usage_meta = cast(JsonObject, raw_usage_meta)

    if raw_candidates:
        raw_candidate = raw_candidates[0]
        if not isinstance(raw_candidate, dict):
            raise GeminiTranslationError("Gemini candidate must be an object")
        choice = translate_candidate_to_openai_choice(
            cast(JsonObject, raw_candidate), _RESPONSE_DIALECT, 0
        )
    else:
        raise_if_prompt_blocked(gemini_json, DEVELOPER_API_DIALECT, provider_label="Gemini")
        choice = {
            "index": 0,
            "message": {"role": "assistant", "content": ""},
            "finish_reason": "stop",
        }

    return {
        "id": f"chatcmpl-gemini-{secrets.token_hex(8)}",
        "object": "chat.completion",
        "model": model,
        "choices": [choice],
        "usage": translate_usage_metadata(usage_meta),
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
                            block_reason = extract_block_reason(chunk_json, DEVELOPER_API_DIALECT)
                            if block_reason is not None:
                                error_chunk = {
                                    "error": {
                                        "type": "provider_response_error",
                                        "message": f"Gemini generation blocked: {block_reason}",
                                    }
                                }
                                yield f"data: {json.dumps(error_chunk)}\n\n"
                                return
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
                            if gemini_finish not in FINISH_REASON_MAP:
                                error_chunk = {
                                    "error": {
                                        "type": "provider_finish_reason",
                                        "message": f"Gemini generation failed: {gemini_finish}",
                                    }
                                }
                                yield f"data: {json.dumps(error_chunk)}\n\n"
                                return
                            final_finish_reason = FINISH_REASON_MAP[gemini_finish]

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
