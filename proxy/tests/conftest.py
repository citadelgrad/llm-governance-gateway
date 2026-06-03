from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import bcrypt
import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from proxy.app.auth import CallerContext, _api_key_cache
from proxy.app.governance_client import InspectResponse
from proxy.app.main import _me_cache, _tenant_cache, app, get_caller
from proxy.app.rate_limit import RateLimitResult

TEST_JWT_SECRET = "test-jwt-secret-for-tests-only-32chars!!"

# Pre-computed for API key auth tests; low rounds so tests stay fast
TEST_API_KEY = "local-fixture-token-for-auth-tests"
TEST_API_KEY_HASH = bcrypt.hashpw(TEST_API_KEY.encode(), bcrypt.gensalt(rounds=4)).decode()

_MODELS_CONFIG = [
    {"id": "gpt-4o-mini", "provider": "openai"},
    {"id": "gpt-4o", "provider": "openai"},
]


def make_jwt(
    user_id: str = "test-user",
    tenant_id: str = "test-tenant",
    roles: list[str] | None = None,
    secret: str = TEST_JWT_SECRET,
) -> str:
    payload = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "roles": roles or ["user"],
        "exp": datetime.now(UTC) + timedelta(hours=1),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def jwt_factory():
    """Returns the make_jwt helper so tests don't need to import from conftest."""
    return make_jwt


@pytest.fixture
def api_key_creds():
    """Returns (raw_key, bcrypt_hash) for API key auth tests."""
    return TEST_API_KEY, TEST_API_KEY_HASH


@pytest.fixture(autouse=True)
def clear_caches():
    _tenant_cache.clear()
    _me_cache.clear()
    _api_key_cache.clear()
    yield
    _tenant_cache.clear()
    _me_cache.clear()
    _api_key_cache.clear()


def _mock_pool(fetchrow_result=None):
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = fetchrow_result
    mock_conn.execute = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=False)

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    pool.close = AsyncMock()
    return pool


def _mock_rate_limiter(allowed: bool = True):
    rl = AsyncMock()
    rl.check.return_value = RateLimitResult(
        allowed=allowed,
        retry_after_seconds=0 if allowed else 60,
        limit=100,
        remaining=99 if allowed else 0,
    )
    return rl


def _setup_app_state(pool, gov_mock):
    """Set app.state directly — ASGITransport does not fire the ASGI lifespan."""
    app.state.db_pool = pool
    app.state.redis = AsyncMock()
    app.state.rate_limiter = _mock_rate_limiter()
    app.state.gov_http = AsyncMock()
    app.state.governance_client = gov_mock
    app.state.openai_client = None
    app.state.models_config = _MODELS_CONFIG
    app.state.models_by_id = {m["id"]: m for m in _MODELS_CONFIG}
    app.state.ready = True


@pytest.fixture
def gov_mock():
    mock = AsyncMock()
    mock.inspect.return_value = InspectResponse(
        decision="allow",
        redacted_text="",
        pii_findings=[],
        harm_score=0.0,
        violations=[],
        audit_id="test-audit-id",
    )
    return mock


@pytest.fixture
async def async_client(gov_mock):
    """ASGI client with get_caller overridden to a plain user."""
    pool = _mock_pool()
    caller = CallerContext(user_id="test-user", tenant_id="test-tenant", roles=["user"])
    app.dependency_overrides[get_caller] = lambda: caller
    _setup_app_state(pool, gov_mock)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, gov_mock

    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client(gov_mock):
    """ASGI client with get_caller overridden to an admin user."""
    pool = _mock_pool()
    caller = CallerContext(user_id="admin-user", tenant_id="test-tenant", roles=["admin"])
    app.dependency_overrides[get_caller] = lambda: caller
    _setup_app_state(pool, gov_mock)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, gov_mock

    app.dependency_overrides.clear()


@pytest.fixture
async def auth_client(gov_mock):
    """ASGI client with NO get_caller override — tests real auth code paths.

    Yields (client, mock_pool) so tests can configure DB mock's fetchrow result.
    """
    pool = _mock_pool()
    _setup_app_state(pool, gov_mock)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, pool
