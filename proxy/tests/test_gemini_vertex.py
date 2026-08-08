from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import google.auth.credentials
import google.auth.exceptions
import httpx
import pytest
from proxy.app.config import Settings
from proxy.app.providers.gemini_vertex import (
    VertexCredentialManager,
    VertexGeminiClient,
    VertexUpstreamError,
    _classify_vertex_error,
    _vertex_base_url,
    _vertex_model_path,
)


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
