"""Upstream error sanitization — normalize non-200 upstream responses.

All providers must use ``sanitize_upstream_error`` instead of forwarding raw
upstream bodies to callers.  Raw bodies may contain provider-internal details
(request IDs, hostnames, stack traces) that must not leak to API consumers.
"""

from __future__ import annotations

import json
import logging

import httpx
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Map HTTP status codes to OpenAI error type strings.
_STATUS_TO_TYPE: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "api_error",
    504: "api_error",
}

# For these statuses, always use the generic message — upstream bodies may contain
# credential details (401/403) or internal infrastructure info (5xx).
_OPAQUE_STATUSES: frozenset[int] = frozenset({401, 403, 500, 502, 503, 504})

_GENERIC_MESSAGE = "An error occurred while communicating with the upstream provider."
_MAX_MESSAGE_LEN = 300


def _error_type_for_status(status_code: int) -> str:
    return _STATUS_TO_TYPE.get(status_code, "api_error")


def _extract_message(raw_body: bytes) -> str:
    """Pull a clean message from a JSON error body; fall back to generic."""
    try:
        data = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return _GENERIC_MESSAGE

    if not isinstance(data, dict):
        return _GENERIC_MESSAGE

    # Nested {"error": {"message": "..."}} shape (OpenAI, Anthropic, Gemini)
    error_obj = data.get("error")
    if isinstance(error_obj, dict):
        msg = error_obj.get("message") or error_obj.get("msg") or error_obj.get("description")
        if msg and isinstance(msg, str):
            return msg[:_MAX_MESSAGE_LEN]

    # Flat shapes: {"message": "..."}, {"detail": "..."}, {"msg": "..."}
    for key in ("message", "detail", "msg", "description", "error"):
        val = data.get(key)
        if val and isinstance(val, str):
            return val[:_MAX_MESSAGE_LEN]

    return _GENERIC_MESSAGE


def sanitize_upstream_error(
    upstream: httpx.Response,
    extra_headers: dict[str, str] | None = None,
    *,
    provider: str = "upstream",
) -> Response:
    """Convert a non-200 upstream response into a sanitized OpenAI-shape error envelope.

    The raw upstream body is logged server-side at ERROR level for debugging but is
    NOT forwarded to the caller.

    Args:
        upstream: The httpx.Response from the upstream provider.
        extra_headers: Optional headers to include in the Starlette response.
        provider: Human-readable provider name for log messages.

    Returns:
        A Starlette ``Response`` with ``Content-Type: application/json`` containing
        the normalized error envelope.
    """
    status_code = upstream.status_code
    raw_body = upstream.content

    # Log full upstream details server-side — never returned to the client.
    logger.error(
        "upstream_error provider=%s status=%d body=%r",
        provider,
        status_code,
        raw_body[:2000],  # cap log size for very large bodies
    )

    error_type = _error_type_for_status(status_code)
    # For auth and server errors, always use the generic message to prevent
    # credential hints or internal hostnames from reaching API consumers.
    message = _GENERIC_MESSAGE if status_code in _OPAQUE_STATUSES else _extract_message(raw_body)

    envelope = {
        "error": {
            "message": message,
            "type": error_type,
            "code": str(status_code),
        }
    }

    return Response(
        content=json.dumps(envelope),
        status_code=status_code,
        media_type="application/json",
        headers=extra_headers or {},
    )
