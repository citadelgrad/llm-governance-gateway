"""Vertex AI-backed Gemini adapter, authenticated via impersonated GCP
service account (never a raw SA key file).

Mirrors the external call shape of proxy/app/providers/gemini.py so
main.py's dispatch code treats both adapters uniformly.
"""

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from typing import cast

import google.auth
import google.auth.credentials
import google.auth.transport.requests
import httpx
from proxy.app.config import Settings
from proxy.app.protocol_types import JsonObject
from proxy.app.provider_capabilities import GEMINI_CHAT_TRANSLATION_FIELDS
from proxy.app.providers._gemini_common import (
    VERTEX_DIALECT,
    GeminiTranslationError,
    extract_block_reason,
    raise_if_prompt_blocked,
    translate_candidate_to_openai_choice,
    translate_chat_request,
    translate_usage_metadata,
)
from proxy.app.providers.errors import sanitize_upstream_error
from starlette.responses import Response, StreamingResponse

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class VertexCredentialManager:
    """Loads and caches an impersonated-ADC credential; refreshes off the
    event loop. Constructed once per client (process lifetime), not per
    request.
    """

    def __init__(self, credentials_path: str | None) -> None:
        self._credentials_path = credentials_path
        self._credentials: google.auth.credentials.Credentials | None = None
        self._project_id: str | None = None
        self._lock = asyncio.Lock()

    def _load_sync(self) -> None:
        # google.auth.default() auto-detects impersonated-service-account
        # ADC JSON when GOOGLE_APPLICATION_CREDENTIALS (or the equivalent
        # gcloud ADC file) points at one. Raises google.auth.exceptions
        # .DefaultCredentialsError if no valid ADC is found.
        credentials, project_id = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
        self._credentials = credentials
        self._project_id = project_id

    async def get_bearer_token(self) -> str:
        async with self._lock:
            if self._credentials is None:
                await asyncio.to_thread(self._load_sync)
            credentials = self._credentials
            assert credentials is not None
            if not credentials.valid:
                # credentials.refresh() is a blocking network call
                # (issues a token request to iamcredentials.googleapis.com
                # for impersonated SAs). google-auth's own .valid/.expired
                # properties already build in a ~3m45s refresh margin, so
                # this only fires when genuinely needed.
                request = google.auth.transport.requests.Request()
                await asyncio.to_thread(credentials.refresh, request)
            token = credentials.token
            assert token is not None
            return token


def _vertex_base_url(settings: Settings) -> str:
    location = settings.gemini_vertex_location
    if location == "global":
        return "https://aiplatform.googleapis.com"
    return f"https://{location}-aiplatform.googleapis.com"


def _vertex_model_path(settings: Settings, model: str) -> str:
    return (
        f"projects/{settings.gemini_vertex_project_id}"
        f"/locations/{settings.gemini_vertex_location}"
        f"/publishers/google/models/{model}"
    )


class VertexUpstreamError(Exception):
    """Raised when Vertex AI returns a non-2xx response to a generateContent
    call. Carries the already-sanitized Response so a future caller (see
    ai-gateway-76iq.11's main.py dispatch) can return it directly."""

    def __init__(self, response: Response) -> None:
        self.response = response
        super().__init__(f"gemini-vertex upstream error (status {response.status_code})")


def _classify_vertex_error(status_code: int, body: dict) -> str:
    """Internal-only classification label for logging; caller-facing response
    stays opaque via the existing sanitize_upstream_error policy.

    403s split into two sub-cases. The impersonation-denial signal
    ("iam.googleapis.com" / "getaccesstoken") is confirmed against a real
    observed error body (service account cannot mint an impersonated token,
    failing at iamcredentials.googleapis.com before Vertex AI is ever
    reached); "impersonat" is kept as a defensive fallback for wording
    variants not covered by that sample. Any other 403 (e.g. an
    impersonated service account that lacks roles/aiplatform.user) falls
    through to the generic IAM-permission-denied classification — that
    reason string is not confirmed by a primary source, so it is
    intentionally not pattern-matched.
    """
    if status_code == 401:
        return "vertex_token_expired"
    if status_code == 403:
        signal = str(body).lower()
        if any(marker in signal for marker in ("iam.googleapis.com", "getaccesstoken", "impersonat")):
            return "vertex_impersonation_denied"
        return "vertex_iam_permission_denied"
    if status_code == 404:
        return "vertex_model_or_region_unavailable"
    if status_code == 429:
        return "vertex_quota_exceeded"
    return "vertex_unknown_error"


def _to_openai_envelope(data: JsonObject, model: str) -> JsonObject:
    """Convert a Vertex AI generateContent response to an OpenAI chat.completion envelope."""
    raw_candidates = data.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise GeminiTranslationError("Vertex candidates must be a list")
    raw_usage_meta = data.get("usageMetadata", {})
    if not isinstance(raw_usage_meta, dict):
        raise GeminiTranslationError("Vertex usageMetadata must be an object")
    usage_meta = cast(JsonObject, raw_usage_meta)

    if raw_candidates:
        raw_candidate = raw_candidates[0]
        if not isinstance(raw_candidate, dict):
            raise GeminiTranslationError("Vertex candidate must be an object")
        choice = translate_candidate_to_openai_choice(
            cast(JsonObject, raw_candidate), VERTEX_DIALECT, 0
        )
    else:
        raise_if_prompt_blocked(data, VERTEX_DIALECT, provider_label="Vertex")
        choice = {
            "index": 0,
            "message": {"role": "assistant", "content": ""},
            "finish_reason": "stop",
        }

    return {
        "id": f"chatcmpl-gemini-vertex-{secrets.token_hex(8)}",
        "object": "chat.completion",
        "model": model,
        "choices": [choice],
        "usage": translate_usage_metadata(usage_meta),
    }


def _translate_chat_body(openai_request: JsonObject) -> tuple[str, JsonObject]:
    """Translate an OpenAI Chat body to a Vertex AI generateContent body, or
    fail before losing request semantics.

    Delegates to _gemini_common.translate_chat_request, the pipeline shared
    with proxy/app/providers/gemini.py's _translate_request, so both Gemini
    adapters reject the same unsupported Chat fields and translate
    tool_choice identically rather than silently dropping it. Unlike
    gemini.py, no default_model is passed: Vertex has no implicit default
    model, so a missing/empty `model` must fail translation.
    """
    return translate_chat_request(openai_request, allowed_fields=GEMINI_CHAT_TRANSLATION_FIELDS)


class VertexGeminiClient:
    """Vertex AI-backed Gemini client. Authenticates via an impersonated GCP
    service account bearer token rather than an API key; the model is
    addressed only via the URL path, never a body field."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._creds = VertexCredentialManager(settings.gemini_vertex_credentials_path or None)
        self._http = httpx.AsyncClient(timeout=settings.gemini_vertex_timeout_seconds)

    async def chat_completions(self, openai_request: JsonObject) -> JsonObject:
        model_value, body = _translate_chat_body(openai_request)

        token = await self._creds.get_bearer_token()
        url = (
            f"{_vertex_base_url(self._settings)}/v1/"
            f"{_vertex_model_path(self._settings, model_value)}:generateContent"
        )
        try:
            upstream = await self._http.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            upstream.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise VertexUpstreamError(
                sanitize_upstream_error(
                    exc.response, provider="gemini-vertex", classify=_classify_vertex_error
                )
            ) from exc

        upstream_json = upstream.json()
        if not isinstance(upstream_json, dict):
            raise GeminiTranslationError("Vertex AI returned a non-object response")
        return _to_openai_envelope(cast(JsonObject, upstream_json), model_value)

    async def chat_completions_stream(self, openai_request: JsonObject) -> AsyncIterator[str]:
        """Stream via streamGenerateContent?alt=sse, yielding OpenAI-compatible
        chat.completion.chunk SSE frames.

        NOTE: the alt=sse framing assumed here (one JSON GenerateContentResponse
        per `data:` line, identical to the Developer API's already-production
        -tested framing) is inferred from convergent secondary sources, not a
        canonical primary-source quote — see docs/spec-gemini-vertex-adapter.md,
        "Open Risks Carried Into Implementation" item 1. Still outstanding: a
        live smoke test against a real Vertex AI streaming endpoint before this
        path is exposed to production traffic.

        Pre-stream failures (bad request, auth, non-200 before any bytes
        arrive) raise VertexUpstreamError, mirroring chat_completions. Once
        streaming has begun, a mid-stream failure (unrecognized finish reason,
        malformed candidate, network error) yields a single SSE-embedded error
        frame and returns without [DONE] — no exception escapes the generator
        after its first yield.
        """
        model_value, body = _translate_chat_body(openai_request)

        token = await self._creds.get_bearer_token()
        url = (
            f"{_vertex_base_url(self._settings)}/v1/"
            f"{_vertex_model_path(self._settings, model_value)}:streamGenerateContent"
        )
        request = self._http.build_request(
            "POST",
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            params={"alt": "sse"},
        )
        upstream = await self._http.send(request, stream=True)
        if upstream.status_code != 200:
            await upstream.aread()
            error = sanitize_upstream_error(
                upstream, provider="gemini-vertex", classify=_classify_vertex_error
            )
            await upstream.aclose()
            raise VertexUpstreamError(error)

        completion_id = f"chatcmpl-gemini-vertex-{secrets.token_hex(8)}"
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
                if not isinstance(chunk_json, dict):
                    continue

                raw_usage_meta = chunk_json.get("usageMetadata")
                if isinstance(raw_usage_meta, dict):
                    usage_meta = cast(JsonObject, raw_usage_meta)

                raw_candidates = chunk_json.get("candidates", [])
                if not isinstance(raw_candidates, list) or not raw_candidates:
                    block_reason = extract_block_reason(chunk_json, VERTEX_DIALECT)
                    if block_reason is not None:
                        error_chunk = {
                            "error": {
                                "type": "provider_response_error",
                                "message": f"Vertex generation blocked: {block_reason}",
                            }
                        }
                        yield f"data: {json.dumps(error_chunk)}\n\n"
                        return
                    continue
                raw_candidate = raw_candidates[0]
                has_finish = isinstance(raw_candidate, dict) and bool(raw_candidate.get("finishReason"))

                try:
                    if not isinstance(raw_candidate, dict):
                        raise GeminiTranslationError("Vertex candidate must be an object")
                    choice = translate_candidate_to_openai_choice(
                        cast(JsonObject, raw_candidate), VERTEX_DIALECT, 0
                    )
                except GeminiTranslationError as exc:
                    error_chunk = {
                        "error": {"type": "provider_response_error", "message": str(exc)}
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                    return

                message = cast(JsonObject, choice["message"])
                tool_calls = message.get("tool_calls")
                delta: JsonObject = {"content": message["content"]}
                if tool_calls:
                    delta["tool_calls"] = tool_calls
                if has_finish or tool_calls:
                    final_finish_reason = choice["finish_reason"]

                oai_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "model": model_value,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
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

        final_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "model": model_value,
            "choices": [{"index": 0, "delta": {}, "finish_reason": final_finish_reason}],
            "usage": translate_usage_metadata(usage_meta),
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    async def aclose(self) -> None:
        await self._http.aclose()


def make_client(settings: Settings) -> VertexGeminiClient:
    return VertexGeminiClient(settings)


async def chat_completions(
    client: VertexGeminiClient,
    body: JsonObject,
    stream: bool,
    extra_headers: dict[str, str],
) -> Response | StreamingResponse:
    """Forward to Vertex AI and return a Starlette Response in OpenAI shape.

    Mirrors proxy/app/providers/gemini.py's module-level chat_completions so
    main.py's dispatch code treats both adapters uniformly.
    """
    if stream:
        gen = client.chat_completions_stream(body)
        try:
            first = await gen.__anext__()
        except VertexUpstreamError as exc:
            exc.response.headers.update(extra_headers)
            return exc.response
        except GeminiTranslationError as exc:
            return Response(
                content=json.dumps(
                    {"error": {"type": "provider_response_error", "message": str(exc)}}
                ),
                status_code=502,
                media_type="application/json",
                headers=extra_headers,
            )

        async def _stream_body():
            yield first
            async for chunk in gen:
                yield chunk

        return StreamingResponse(
            _stream_body(),
            status_code=200,
            media_type="text/event-stream",
            headers=extra_headers,
        )

    try:
        envelope = await client.chat_completions(body)
    except VertexUpstreamError as exc:
        exc.response.headers.update(extra_headers)
        return exc.response
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
