from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import google.auth.credentials
import google.auth.exceptions
import pytest
from proxy.app.config import Settings
from proxy.app.providers.gemini_vertex import (
    VertexCredentialManager,
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
