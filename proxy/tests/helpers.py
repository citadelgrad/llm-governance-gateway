from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from proxy.app.governance_client import InspectResponse
from proxy.app.rate_limit import RateLimitResult


def make_mock_pool():
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None
    mock_conn.execute = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=False)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    pool.close = AsyncMock()
    return pool


def make_mock_rate_limiter():
    rl = AsyncMock()
    rl.check.return_value = RateLimitResult(
        allowed=True, retry_after_seconds=0, limit=100, remaining=99
    )
    return rl


def make_gov_mock(audit_id: str = "pbt-audit-id"):
    mock = AsyncMock()
    mock.inspect.return_value = InspectResponse(
        decision="allow",
        redacted_text="",
        pii_findings=[],
        harm_score=0.0,
        violations=[],
        audit_id=audit_id,
    )
    return mock
