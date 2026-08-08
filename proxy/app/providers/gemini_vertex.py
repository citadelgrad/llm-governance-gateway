"""Vertex AI-backed Gemini adapter, authenticated via impersonated GCP
service account (never a raw SA key file).

Mirrors the external call shape of proxy/app/providers/gemini.py so
main.py's dispatch code treats both adapters uniformly.
"""

import asyncio
import secrets
from typing import cast

import google.auth
import google.auth.credentials
import google.auth.transport.requests
import httpx
from proxy.app.config import Settings
from proxy.app.protocol_types import JsonObject
from proxy.app.providers._gemini_common import (
    VERTEX_DIALECT,
    GeminiTranslationError,
    extract_message_text,
    is_block_reason_unset,
    translate_candidate_to_openai_choice,
    translate_generation_config,
    translate_openai_messages_to_contents,
    translate_tools,
    translate_usage_metadata,
)
from proxy.app.providers.errors import sanitize_upstream_error
from starlette.responses import Response

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

    TODO(ai-gateway-76iq.7): replace with the real 401/403/404/429
    classification heuristic once sanitize_upstream_error grows an optional
    `classify` parameter to thread this label through.
    """
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
        raw_prompt_feedback = data.get("promptFeedback", {})
        block_reason = (
            raw_prompt_feedback.get("blockReason") if isinstance(raw_prompt_feedback, dict) else None
        )
        if not is_block_reason_unset(block_reason, VERTEX_DIALECT):
            raise GeminiTranslationError(f"Vertex generation blocked: {block_reason}")
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


class VertexGeminiClient:
    """Vertex AI-backed Gemini client. Authenticates via an impersonated GCP
    service account bearer token rather than an API key; the model is
    addressed only via the URL path, never a body field."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._creds = VertexCredentialManager(settings.gemini_vertex_credentials_path or None)
        self._http = httpx.AsyncClient(timeout=settings.gemini_vertex_timeout_seconds)

    async def chat_completions(self, openai_request: JsonObject) -> JsonObject:
        model_value = openai_request.get("model")
        if not isinstance(model_value, str) or not model_value:
            raise GeminiTranslationError("model must be a string")
        messages = openai_request.get("messages", [])
        if not isinstance(messages, list):
            raise GeminiTranslationError("messages must be a list")

        contents = translate_openai_messages_to_contents(cast("list[JsonObject]", messages))

        system_parts = [
            extract_message_text(message.get("content"), location=f"message {message_index}")
            for message_index, message in enumerate(messages)
            if isinstance(message, dict) and message.get("role") in {"system", "developer"}
        ]

        body: JsonObject = {"contents": contents}
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}

        generation_config = translate_generation_config(openai_request)
        if generation_config:
            body["generationConfig"] = generation_config

        tools = translate_tools(openai_request.get("tools"))
        if tools is not None:
            body["tools"] = tools

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
            # TODO(ai-gateway-76iq.7): pass classify=_classify_vertex_error once
            # sanitize_upstream_error grows an optional classify parameter.
            raise VertexUpstreamError(
                sanitize_upstream_error(exc.response, provider="gemini-vertex")
            ) from exc

        upstream_json = upstream.json()
        if not isinstance(upstream_json, dict):
            raise GeminiTranslationError("Vertex AI returned a non-object response")
        return _to_openai_envelope(cast(JsonObject, upstream_json), model_value)


def make_client(settings: Settings) -> VertexGeminiClient:
    return VertexGeminiClient(settings)
