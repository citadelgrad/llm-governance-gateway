from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from google.auth import exceptions as auth_exceptions

from app import google_credentials


class _Credentials:
    def __init__(self, service_account_email: str | None = None, error: Exception | None = None):
        self.service_account_email = service_account_email
        self.error = error
        self.expiry = None
        self.refresh_calls = 0

    def refresh(self, _request) -> None:
        self.refresh_calls += 1
        if self.error:
            raise self.error
        self.expiry = datetime.now(UTC) + timedelta(hours=1)


def test_preflight_refreshes_and_returns_expected_impersonated_identity(monkeypatch):
    credentials = _Credentials("gateway-dlp@example.iam.gserviceaccount.com")
    monkeypatch.setattr(google_credentials, "ImpersonatedCredentials", _Credentials)
    monkeypatch.setattr(
        google_credentials.google.auth,
        "default",
        lambda **_kwargs: (credentials, "example"),
    )
    monkeypatch.setattr(google_credentials, "Request", lambda: object())

    result = google_credentials.preflight(
        expected_service_account="gateway-dlp@example.iam.gserviceaccount.com"
    )

    assert result.service_account_email == "gateway-dlp@example.iam.gserviceaccount.com"
    assert result.credentials is credentials
    assert result.expiry is credentials.expiry
    assert credentials.refresh_calls == 1


def test_preflight_rejects_personal_adc_when_service_account_is_required(monkeypatch):
    credentials = _Credentials()
    monkeypatch.setattr(
        google_credentials.google.auth,
        "default",
        lambda **_kwargs: (credentials, "example"),
    )

    with pytest.raises(
        google_credentials.GoogleCredentialPreflightError,
        match="must use service-account impersonation",
    ):
        google_credentials.preflight(
            expected_service_account="gateway-dlp@example.iam.gserviceaccount.com"
        )

    assert credentials.refresh_calls == 0


def test_preflight_rejects_wrong_service_account_without_refreshing(monkeypatch):
    credentials = _Credentials("other@example.iam.gserviceaccount.com")
    monkeypatch.setattr(google_credentials, "ImpersonatedCredentials", _Credentials)
    monkeypatch.setattr(
        google_credentials.google.auth,
        "default",
        lambda **_kwargs: (credentials, "example"),
    )

    with pytest.raises(
        google_credentials.GoogleCredentialPreflightError,
        match="does not match GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT",
    ):
        google_credentials.preflight(
            expected_service_account="gateway-dlp@example.iam.gserviceaccount.com"
        )

    assert credentials.refresh_calls == 0


def test_preflight_sanitizes_expired_source_credential_error(monkeypatch):
    credentials = _Credentials(
        "gateway-dlp@example.iam.gserviceaccount.com",
        auth_exceptions.RefreshError("upstream token and credential details"),
    )
    monkeypatch.setattr(google_credentials, "ImpersonatedCredentials", _Credentials)
    monkeypatch.setattr(
        google_credentials.google.auth,
        "default",
        lambda **_kwargs: (credentials, "example"),
    )
    monkeypatch.setattr(google_credentials, "Request", lambda: object())

    with pytest.raises(
        google_credentials.GoogleCredentialPreflightError,
        match="expired or require reauthentication",
    ) as exc_info:
        google_credentials.preflight(
            expected_service_account="gateway-dlp@example.iam.gserviceaccount.com"
        )

    assert "upstream token" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_preflight_rejects_long_lived_service_account_key_credentials(monkeypatch):
    credentials = _Credentials("gateway-dlp@example.iam.gserviceaccount.com")
    monkeypatch.setattr(
        google_credentials.google.auth,
        "default",
        lambda **_kwargs: (credentials, "example"),
    )

    with pytest.raises(
        google_credentials.GoogleCredentialPreflightError,
        match="private-key credentials are rejected",
    ):
        google_credentials.preflight(
            expected_service_account="gateway-dlp@example.iam.gserviceaccount.com"
        )

    assert credentials.refresh_calls == 0


def test_preflight_sanitizes_missing_adc_error(monkeypatch):
    def missing(**_kwargs):
        raise auth_exceptions.DefaultCredentialsError("secret credential path")

    monkeypatch.setattr(google_credentials.google.auth, "default", missing)

    with pytest.raises(
        google_credentials.GoogleCredentialPreflightError,
        match="not configured or unavailable",
    ) as exc_info:
        google_credentials.preflight()

    assert "secret credential path" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
