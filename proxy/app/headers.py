from collections.abc import Sequence
from datetime import datetime

from proxy.app.protocol_types import JsonObject


def rate_limit_headers(
    limit: int,
    remaining: int,
    reset_at: datetime,
) -> dict[str, str]:
    return {
        "x-ratelimit-limit-requests": str(limit),
        "x-ratelimit-remaining-requests": str(remaining),
        "x-ratelimit-reset-requests": reset_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def retry_headers(retry_after_seconds: int) -> dict[str, str]:
    return {
        "Retry-After": str(retry_after_seconds),
        "retry-after-ms": str(retry_after_seconds * 1000),
    }


def pii_headers(
    pii_types: list[str],
    notification_mode: str,
) -> dict[str, str]:
    if notification_mode == "silent":
        return {}
    return {
        "x-gateway-pii-redacted": "true",
        "x-gateway-pii-types": ",".join(pii_types),
    }


def error_envelope(
    error_type: str,
    message: str,
    violations: Sequence[str] = (),
    required_roles: Sequence[str] = (),
    approved_providers_for_classification: Sequence[str] = (),
    details: JsonObject | None = None,
) -> JsonObject:
    body: JsonObject = {
        "type": error_type,
        "message": message,
        "violations": list(violations),
    }
    if required_roles:
        body["required_roles"] = list(required_roles)
    if approved_providers_for_classification:
        body["approved_providers_for_classification"] = list(approved_providers_for_classification)
    if details is not None:
        body["details"] = details
    return {"error": body}
