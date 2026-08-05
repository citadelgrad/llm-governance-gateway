from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import google.auth
from google.auth import exceptions as auth_exceptions
from google.auth.impersonated_credentials import Credentials as ImpersonatedCredentials
from google.auth.transport.requests import Request

_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GoogleCredentialPreflightError(RuntimeError):
    """Sanitized local/runtime ADC preflight failure."""


@dataclass(frozen=True)
class GoogleCredentialPreflight:
    credentials: Any
    service_account_email: str | None
    expiry: datetime | None


def preflight(*, expected_service_account: str | None = None) -> GoogleCredentialPreflight:
    """Load and refresh ADC, optionally enforcing the effective service account.

    The caller gets a credential object that has already completed one refresh.
    Exceptions deliberately exclude provider diagnostics, token data, and paths.
    """
    try:
        credentials, _ = google.auth.default(scopes=(_CLOUD_PLATFORM_SCOPE,))
    except auth_exceptions.DefaultCredentialsError:
        raise GoogleCredentialPreflightError(
            "Google ADC is not configured or unavailable"
        ) from None

    service_account_email = getattr(credentials, "service_account_email", None)
    if expected_service_account:
        if not isinstance(credentials, ImpersonatedCredentials):
            raise GoogleCredentialPreflightError(
                "Google ADC must use service-account impersonation; private-key credentials are rejected"
            )
        if not service_account_email:
            raise GoogleCredentialPreflightError(
                "Google ADC does not identify an impersonated service account"
            )
        if service_account_email != expected_service_account:
            raise GoogleCredentialPreflightError(
                "Google ADC identity does not match GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT"
            )

    try:
        credentials.refresh(Request())
    except auth_exceptions.RefreshError:
        raise GoogleCredentialPreflightError(
            "Google ADC source credentials are expired or require reauthentication"
        ) from None
    except Exception:
        raise GoogleCredentialPreflightError("Google ADC refresh failed") from None

    return GoogleCredentialPreflight(
        credentials=credentials,
        service_account_email=service_account_email,
        expiry=getattr(credentials, "expiry", None),
    )
