"""Vertex AI-backed Gemini adapter, authenticated via impersonated GCP
service account (never a raw SA key file).

Mirrors the external call shape of proxy/app/providers/gemini.py so
main.py's dispatch code treats both adapters uniformly.
"""

import asyncio

import google.auth
import google.auth.credentials
import google.auth.transport.requests
from proxy.app.config import Settings

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
