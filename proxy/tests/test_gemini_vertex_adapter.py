from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import google.auth.credentials
import google.auth.exceptions
import httpx
import pytest
from proxy.app.config import Settings
from proxy.app.providers import gemini_vertex
from proxy.app.providers._gemini_common import GeminiTranslationError
from proxy.app.providers.gemini_vertex import (
    VertexCredentialManager,
    VertexGeminiClient,
    VertexUpstreamError,
    _classify_vertex_error,
    _vertex_base_url,
    _vertex_model_path,
)
from starlette.responses import StreamingResponse


class FakeCredentials(google.auth.credentials.Credentials):
    """A real Credentials subclass so `.valid`/`.expired` use the base
    class's actual token/expiry-based computation rather than a stand-in."""

    def __init__(self, *, token: str | None = None, refresh_delay: float = 0.0) -> None:
        super().__init__()
        self.token = token
        self.refresh_calls = 0
        self.refresh_threads: list[int] = []
        self._refresh_delay = refresh_delay

    def refresh(self, request: object) -> None:
        if self._refresh_delay:
            time.sleep(self._refresh_delay)
        self.refresh_calls += 1
        self.refresh_threads.append(threading.get_ident())
        self.token = "refreshed-token"


def _settings(*, project_id: str = "proj-1", location: str = "us-central1") -> Settings:
    return Settings(gemini_vertex_project_id=project_id, gemini_vertex_location=location)  # pyright: ignore[reportCallIssue]


# ---------------------------------------------------------------------------
# VertexCredentialManager.get_bearer_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_load_calls_google_auth_default():
    fake_creds = FakeCredentials(token="loaded-token")
    manager = VertexCredentialManager(credentials_path=None)

    with patch(
        "proxy.app.providers.gemini_vertex.google.auth.default",
        return_value=(fake_creds, "proj-1"),
    ) as mock_default:
        token = await manager.get_bearer_token()

    mock_default.assert_called_once()
    assert manager._credentials is fake_creds
    assert token == "loaded-token"


@pytest.mark.asyncio
async def test_cached_valid_token_skips_refresh():
    fake_creds = FakeCredentials(token="already-valid-token")
    manager = VertexCredentialManager(credentials_path=None)
    manager._credentials = fake_creds

    with patch("proxy.app.providers.gemini_vertex.google.auth.default") as mock_default:
        token = await manager.get_bearer_token()

    mock_default.assert_not_called()
    assert fake_creds.refresh_calls == 0
    assert token == "already-valid-token"


@pytest.mark.asyncio
async def test_expired_token_refreshes_off_the_event_loop():
    fake_creds = FakeCredentials(token="stale-token")
    fake_creds.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    manager = VertexCredentialManager(credentials_path=None)
    manager._credentials = fake_creds

    token = await manager.get_bearer_token()

    assert fake_creds.refresh_calls == 1
    assert token == "refreshed-token"
    # asyncio.to_thread runs in a worker thread, never the event loop thread
    assert fake_creds.refresh_threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_missing_adc_raises_default_credentials_error():
    manager = VertexCredentialManager(credentials_path=None)

    with (
        patch(
            "proxy.app.providers.gemini_vertex.google.auth.default",
            side_effect=google.auth.exceptions.DefaultCredentialsError("no ADC found"),
        ),
        pytest.raises(google.auth.exceptions.DefaultCredentialsError),
    ):
        await manager.get_bearer_token()


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_duplicate_refresh():
    fake_creds = FakeCredentials(token=None, refresh_delay=0.05)
    manager = VertexCredentialManager(credentials_path=None)
    manager._credentials = fake_creds

    tokens = await asyncio.gather(
        manager.get_bearer_token(),
        manager.get_bearer_token(),
    )

    assert fake_creds.refresh_calls == 1
    assert tokens == ["refreshed-token", "refreshed-token"]


# ---------------------------------------------------------------------------
# _vertex_base_url / _vertex_model_path
# ---------------------------------------------------------------------------


def test_vertex_base_url_regional():
    settings = _settings(location="us-central1")
    assert _vertex_base_url(settings) == "https://us-central1-aiplatform.googleapis.com"


def test_vertex_base_url_global():
    settings = _settings(location="global")
    assert _vertex_base_url(settings) == "https://aiplatform.googleapis.com"


def test_vertex_model_path():
    settings = _settings(project_id="my-proj", location="us-east4")
    assert _vertex_model_path(settings, "gemini-1.5-pro") == (
        "projects/my-proj/locations/us-east4/publishers/google/models/gemini-1.5-pro"
    )


# ---------------------------------------------------------------------------
# _classify_vertex_error
# ---------------------------------------------------------------------------

# Confirmed real 403 body (deep-research.md §5.3a): the service account
# cannot mint an impersonated token — failure happens at
# iamcredentials.googleapis.com's generateAccessToken, before Vertex AI is
# ever reached. Notably contains neither "iamcredentials" nor "impersonat"
# as a substring, which is why the classifier keys off "iam.googleapis.com"
# / "getaccesstoken" instead.
_IMPERSONATION_DENIAL_BODY = {
    "error": {
        "code": 403,
        "message": "Permission 'iam.serviceAccounts.getAccessToken' denied on resource (or it may not exist).",
        "status": "PERMISSION_DENIED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "IAM_PERMISSION_DENIED",
                "domain": "iam.googleapis.com",
                "metadata": {"permission": "iam.serviceAccounts.getAccessToken"},
            }
        ],
    }
}


def test_classify_401_as_token_expired():
    assert _classify_vertex_error(401, {"error": {"message": "invalid_grant"}}) == (
        "vertex_token_expired"
    )


def test_classify_403_impersonation_denial_from_confirmed_body():
    assert _classify_vertex_error(403, _IMPERSONATION_DENIAL_BODY) == "vertex_impersonation_denied"


def test_classify_403_without_impersonation_signal_as_iam_permission_denied():
    body = {
        "error": {
            "code": 403,
            "message": "Permission 'aiplatform.endpoints.predict' denied on resource.",
            "status": "PERMISSION_DENIED",
        }
    }
    assert _classify_vertex_error(403, body) == "vertex_iam_permission_denied"


def test_classify_404_as_model_or_region_unavailable():
    body = {"error": {"message": "Publisher Model was not found or your project does not have access to it."}}
    assert _classify_vertex_error(404, body) == "vertex_model_or_region_unavailable"


def test_classify_429_as_quota_exceeded():
    body = {
        "error": {
            "code": 429,
            "message": "Quota exceeded for aiplatform.googleapis.com/generate_content_requests_per_minute.",
            "status": "RESOURCE_EXHAUSTED",
        }
    }
    assert _classify_vertex_error(429, body) == "vertex_quota_exceeded"


def test_classify_unmapped_status_as_unknown():
    assert _classify_vertex_error(418, {}) == "vertex_unknown_error"


# ---------------------------------------------------------------------------
# _to_openai_envelope — promptFeedback.blockReason
# ---------------------------------------------------------------------------
#
# The raises-on-blocked-prompt / defaults-to-stop-when-unset behavior is
# covered for both this adapter and gemini.py's in a single shared
# parametrized test — see test_gemini_common.py's
# test_to_openai_envelope_raises_on_blocked_prompt /
# test_to_openai_envelope_default_stop_when_block_reason_unset.


# ---------------------------------------------------------------------------
# VertexGeminiClient.chat_completions
# ---------------------------------------------------------------------------


def _vertex_client_with_transport(handler, *, project_id="proj-1", location="us-central1", token="fake-token"):
    settings = _settings(project_id=project_id, location=location)
    client = VertexGeminiClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._creds._credentials = FakeCredentials(token=token)
    return client


_HAPPY_PATH_JSON = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [{"text": "hello there"}]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 10,
        "candidatesTokenCount": 5,
        "totalTokenCount": 15,
    },
}


@pytest.mark.asyncio
async def test_chat_completions_request_body_omits_model_and_addresses_via_url():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json=_HAPPY_PATH_JSON)

    client = _vertex_client_with_transport(handler)
    await client.chat_completions(
        {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    request = captured["request"]
    sent_body = json.loads(request.content)
    assert "model" not in sent_body
    assert request.url.path == (
        "/v1/projects/proj-1/locations/us-central1/publishers/google/models/"
        "gemini-1.5-pro:generateContent"
    )


@pytest.mark.asyncio
async def test_chat_completions_returns_openai_envelope_with_mapped_usage():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_HAPPY_PATH_JSON)

    client = _vertex_client_with_transport(handler)
    result = await client.chat_completions(
        {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    assert result["choices"][0]["message"]["content"] == "hello there"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }


@pytest.mark.asyncio
async def test_chat_completions_wraps_non_2xx_response_without_leaking_body():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": "caller lacks permission on projects/proj-1/super-secret-detail"
                }
            },
        )

    client = _vertex_client_with_transport(handler)

    with pytest.raises(VertexUpstreamError) as exc_info:
        await client.chat_completions(
            {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
        )

    err = exc_info.value
    assert err.response.status_code == 403
    assert b"super-secret-detail" not in err.response.body
    assert "super-secret-detail" not in str(err)


@pytest.mark.asyncio
async def test_chat_completions_classifies_impersonation_denial_without_leaking_label(caplog):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=_IMPERSONATION_DENIAL_BODY)

    client = _vertex_client_with_transport(handler)

    with (
        caplog.at_level("ERROR"),
        pytest.raises(VertexUpstreamError) as exc_info,
    ):
        await client.chat_completions(
            {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
        )

    err = exc_info.value
    body = json.loads(err.response.body)
    # Caller-facing response stays the generic opaque 403 envelope — the
    # classification label must never appear in it.
    assert set(body["error"].keys()) == {"message", "type", "code"}
    assert "vertex_impersonation_denied" not in err.response.body.decode()
    assert "getAccessToken" not in err.response.body.decode()
    # The classification is captured server-side for internal observability.
    assert "classification=vertex_impersonation_denied" in caplog.text


@pytest.mark.asyncio
async def test_chat_completions_attaches_bearer_token_header():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=_HAPPY_PATH_JSON)

    client = _vertex_client_with_transport(handler, token="secret-token-xyz")
    await client.chat_completions(
        {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    assert captured["authorization"] == "Bearer secret-token-xyz"


# _collect_stream (defined further below, under chat_completions_stream) is
# forward-referenced here so the two tests below can dispatch through either
# VertexGeminiClient.chat_completions or .chat_completions_stream — the two
# request-translation behaviors they exercise (unsupported-field rejection,
# tool_choice translation) are identical regardless of which entry point is
# used, so a single parametrized test covers both instead of hand-duplicating
# the body for each.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dispatch",
    [
        lambda client, request: client.chat_completions(request),
        lambda client, request: _collect_stream(client, request),
    ],
    ids=["sync", "stream"],
)
async def test_chat_completions_rejects_unsupported_field_before_dispatch(dispatch):
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made for a request-shape error")

    client = _vertex_client_with_transport(handler)

    with pytest.raises(GeminiTranslationError, match="frequency_penalty"):
        await dispatch(
            client,
            {
                "model": "gemini-1.5-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "frequency_penalty": 0.5,
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_response", "dispatch"),
    [
        (
            lambda: httpx.Response(200, json=_HAPPY_PATH_JSON),
            lambda client, request: client.chat_completions(request),
        ),
        (
            lambda: _sse_response(
                [
                    'data: {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}',
                    "data: [DONE]",
                ]
            ),
            lambda client, request: _collect_stream(client, request),
        ),
    ],
    ids=["sync", "stream"],
)
async def test_chat_completions_translates_tool_choice_to_tool_config(make_response, dispatch):
    """Previously gemini_vertex.py silently dropped tool_choice entirely —
    it must now translate it identically to gemini.py rather than losing
    the caller's tool-selection intent. Parametrized over both the
    non-streaming and streaming entry points since translation happens
    before dispatch and behaves identically either way."""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return make_response()

    client = _vertex_client_with_transport(handler)
    await dispatch(
        client,
        {
            "model": "gemini-1.5-pro",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "description": "d"},
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
        },
    )

    sent_body = json.loads(captured["request"].content)
    assert sent_body["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["lookup"]}
    }


# ---------------------------------------------------------------------------
# VertexGeminiClient.chat_completions_stream
# ---------------------------------------------------------------------------


def _sse_response(lines: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        content="\n".join(lines).encode(),
        headers={"content-type": "text/event-stream"},
    )


async def _collect_stream(client: VertexGeminiClient, request: dict) -> list[dict]:
    frames = []
    async for frame in client.chat_completions_stream(request):
        assert frame.startswith("data: ")
        assert frame.endswith("\n\n")
        raw = frame[len("data: ") : -2]
        frames.append(raw if raw == "[DONE]" else json.loads(raw))
    return frames


@pytest.mark.asyncio
async def test_chat_completions_stream_uses_alt_sse_on_stream_generate_content_url():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return _sse_response(
            ['data: {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}', "data: [DONE]"]
        )

    client = _vertex_client_with_transport(handler)
    await _collect_stream(
        client, {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    request = captured["request"]
    assert request.url.path == (
        "/v1/projects/proj-1/locations/us-central1/publishers/google/models/"
        "gemini-1.5-pro:streamGenerateContent"
    )
    assert request.url.params["alt"] == "sse"


@pytest.mark.asyncio
async def test_chat_completions_stream_yields_chunks_in_arrival_order():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                'data: {"candidates": [{"content": {"parts": [{"text": "one "}]}}]}',
                'data: {"candidates": [{"content": {"parts": [{"text": "two "}]}}]}',
                'data: {"candidates": [{"content": {"parts": [{"text": "three"}]}, "finishReason": "STOP"}]}',
                "data: [DONE]",
            ]
        )

    client = _vertex_client_with_transport(handler)
    frames = await _collect_stream(
        client, {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    contents = [f["choices"][0]["delta"]["content"] for f in frames[:3]]
    assert contents == ["one ", "two ", "three"]


@pytest.mark.asyncio
async def test_chat_completions_stream_terminates_with_done_after_final_usage_chunk():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                'data: {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1, "totalTokenCount": 4}}',
                "data: [DONE]",
            ]
        )

    client = _vertex_client_with_transport(handler)
    frames = await _collect_stream(
        client, {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    assert frames[-1] == "[DONE]"
    final_chunk = frames[-2]
    assert final_chunk["choices"][0]["finish_reason"] == "stop"
    assert final_chunk["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 1,
        "total_tokens": 4,
    }


@pytest.mark.asyncio
async def test_chat_completions_stream_interim_chunks_do_not_report_finish_reason():
    """Guards against translate_candidate_to_openai_choice's default-to-STOP
    behavior leaking into per-token streaming deltas."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                'data: {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}',
                'data: {"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]}',
                "data: [DONE]",
            ]
        )

    client = _vertex_client_with_transport(handler)
    frames = await _collect_stream(
        client, {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    assert frames[0]["choices"][0]["finish_reason"] is None
    assert frames[1]["choices"][0]["finish_reason"] is None
    assert frames[2]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_chat_completions_stream_tool_call_sets_finish_reason():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                'data: {"candidates": [{"content": {"parts": [{"functionCall": {"name": "lookup", "args": {"q": "x"}}}]}}]}',
                "data: [DONE]",
            ]
        )

    client = _vertex_client_with_transport(handler)
    frames = await _collect_stream(
        client, {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    tool_call_chunk = frames[0]
    assert tool_call_chunk["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "lookup"
    final_chunk = frames[-2]
    assert final_chunk["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_chat_completions_stream_pre_stream_non_200_raises_vertex_upstream_error():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"message": "caller lacks permission on secret-resource"}}
        )

    client = _vertex_client_with_transport(handler)

    with pytest.raises(VertexUpstreamError) as exc_info:
        await _collect_stream(
            client, {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
        )

    err = exc_info.value
    assert err.response.status_code == 403
    assert b"secret-resource" not in err.response.body


@pytest.mark.asyncio
async def test_chat_completions_stream_unrecognized_finish_reason_yields_sse_error_without_done():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                'data: {"candidates": [{"content": {"parts": [{"text": "uh oh"}]}, "finishReason": "OTHER"}]}',
                "data: [DONE]",
            ]
        )

    client = _vertex_client_with_transport(handler)
    frames = await _collect_stream(
        client, {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    assert frames[-1]["error"]["type"] == "provider_response_error"
    assert "[DONE]" not in frames


@pytest.mark.asyncio
async def test_chat_completions_stream_mid_stream_request_error_yields_sse_error_without_done():
    """A network failure once bytes are already arriving (status already
    committed to 200) must surface as an SSE-embedded error frame, not an
    exception escaping an already-yielding generator."""

    async def body() -> AsyncIterator[bytes]:
        yield b'data: {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}\n\n'
        raise httpx.ReadError("connection reset")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    client = _vertex_client_with_transport(handler)
    frames = await _collect_stream(
        client, {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    assert frames[0]["choices"][0]["delta"]["content"] == "partial"
    assert frames[-1]["error"]["type"] == "upstream_connection_error"
    assert "[DONE]" not in frames


@pytest.mark.asyncio
async def test_chat_completions_stream_blocked_prompt_yields_sse_error_without_done():
    """A candidates-less chunk that carries a genuine promptFeedback.block
    Reason must terminate the stream with an SSE error frame, not silently
    complete as a normal empty 'stop' response."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                'data: {"promptFeedback": {"blockReason": "SAFETY"}}',
                "data: [DONE]",
            ]
        )

    client = _vertex_client_with_transport(handler)
    frames = await _collect_stream(
        client, {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]}
    )

    assert len(frames) == 1
    assert "Vertex generation blocked: SAFETY" in frames[0]["error"]["message"]
    assert "[DONE]" not in frames


# ---------------------------------------------------------------------------
# VertexGeminiClient.aclose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_closes_underlying_http_client():
    settings = _settings()
    client = VertexGeminiClient(settings)

    await client.aclose()

    assert client._http.is_closed


# ---------------------------------------------------------------------------
# module-level chat_completions (main.py's dispatch call shape)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_module_chat_completions_non_streaming_success_returns_openai_response():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_HAPPY_PATH_JSON)

    client = _vertex_client_with_transport(handler)
    response = await gemini_vertex.chat_completions(
        client,
        {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]},
        False,
        {"X-Audit-ID": "audit-1"},
    )

    assert not isinstance(response, StreamingResponse)
    assert response.status_code == 200
    assert response.headers["x-audit-id"] == "audit-1"
    body = json.loads(response.body)
    assert body["choices"][0]["message"]["content"] == "hello there"


@pytest.mark.asyncio
async def test_module_chat_completions_non_streaming_upstream_error_merges_headers():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"message": "caller lacks permission on secret-resource"}}
        )

    client = _vertex_client_with_transport(handler)
    response = await gemini_vertex.chat_completions(
        client,
        {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]},
        False,
        {"X-Audit-ID": "audit-1"},
    )

    assert not isinstance(response, StreamingResponse)
    assert response.status_code == 403
    assert response.headers["x-audit-id"] == "audit-1"
    assert b"secret-resource" not in response.body


@pytest.mark.asyncio
async def test_module_chat_completions_non_streaming_translation_error_returns_502():
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made for a request-shape error")

    client = _vertex_client_with_transport(handler)
    response = await gemini_vertex.chat_completions(
        client,
        {"model": 123, "messages": []},
        False,
        {},
    )

    assert not isinstance(response, StreamingResponse)
    assert response.status_code == 502
    body = json.loads(response.body)
    assert body["error"]["type"] == "provider_response_error"


@pytest.mark.asyncio
async def test_module_chat_completions_streaming_success_returns_streaming_response():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                'data: {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}',
                "data: [DONE]",
            ]
        )

    client = _vertex_client_with_transport(handler)
    response = await gemini_vertex.chat_completions(
        client,
        {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]},
        True,
        {"X-Audit-ID": "audit-1"},
    )

    assert isinstance(response, StreamingResponse)
    assert response.headers["x-audit-id"] == "audit-1"
    chunks = [
        chunk if isinstance(chunk, str) else chunk.decode()
        async for chunk in response.body_iterator
    ]
    content = "".join(chunks)
    assert '"content": "hi"' in content
    assert "data: [DONE]" in content


@pytest.mark.asyncio
async def test_module_chat_completions_streaming_pre_stream_upstream_error_returns_plain_response():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"message": "caller lacks permission on secret-resource"}}
        )

    client = _vertex_client_with_transport(handler)
    response = await gemini_vertex.chat_completions(
        client,
        {"model": "gemini-1.5-pro", "messages": [{"role": "user", "content": "hi"}]},
        True,
        {"X-Audit-ID": "audit-1"},
    )

    assert not isinstance(response, StreamingResponse)
    assert response.status_code == 403
    assert response.headers["x-audit-id"] == "audit-1"
    assert b"secret-resource" not in response.body


@pytest.mark.asyncio
async def test_module_chat_completions_streaming_pre_stream_translation_error_returns_502():
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made for a request-shape error")

    client = _vertex_client_with_transport(handler)
    response = await gemini_vertex.chat_completions(
        client,
        {"model": 123, "messages": []},
        True,
        {},
    )

    assert not isinstance(response, StreamingResponse)
    assert response.status_code == 502
    body = json.loads(response.body)
    assert body["error"]["type"] == "provider_response_error"
