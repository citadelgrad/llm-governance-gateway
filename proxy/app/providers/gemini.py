from __future__ import annotations

import json
from dataclasses import replace
from typing import cast

import httpx
from proxy.app.protocol_types import JsonObject
from proxy.app.provider_capabilities import GEMINI_CHAT_TRANSLATION_FIELDS
from proxy.app.providers._gemini_common import (
    DEVELOPER_API_DIALECT,
    GeminiTranslationError,
    iter_openai_chat_sse_from_gemini_lines,
    translate_chat_request,
    translate_generate_content_response_to_openai_envelope,
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
    return translate_generate_content_response_to_openai_envelope(
        gemini_json,
        model=model,
        dialect=_RESPONSE_DIALECT,
        provider_label="Gemini",
        completion_id_prefix="chatcmpl-gemini-",
    )


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
            try:
                async for frame in iter_openai_chat_sse_from_gemini_lines(
                    upstream.aiter_lines(),
                    model=model,
                    dialect=_RESPONSE_DIALECT,
                    provider_label="Gemini",
                    completion_id_prefix="chatcmpl-gemini-",
                ):
                    yield frame
            except httpx.TimeoutException:
                yield 'data: {"error": {"type": "upstream_timeout"}}\n\n'
                return
            except httpx.RequestError:
                yield 'data: {"error": {"type": "upstream_connection_error"}}\n\n'
                return
            finally:
                await upstream.aclose()

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
