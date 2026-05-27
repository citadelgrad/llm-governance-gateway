from datetime import datetime


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
        "X-Gateway-Pii-Redacted": "true",
        "X-Gateway-Pii-Types": ",".join(pii_types),
    }


def error_envelope(
    error_type: str,
    message: str,
    violations: list[str] = (),
    required_roles: list[str] = (),
    approved_providers_for_classification: list[str] = (),
) -> dict:
    body: dict = {
        "type": error_type,
        "message": message,
        "violations": list(violations),
    }
    if required_roles:
        body["required_roles"] = list(required_roles)
    if approved_providers_for_classification:
        body["approved_providers_for_classification"] = list(approved_providers_for_classification)
    return {"error": body}
