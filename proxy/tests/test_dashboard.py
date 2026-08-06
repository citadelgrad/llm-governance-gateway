from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from proxy.app.auth import CallerContext
from proxy.app.dashboard import get_dashboard_caller
from proxy.app.main import app
from proxy.tests.conftest import _mock_pool, _setup_app_state

_MODEL_ROWS = [
    {
        "model_id": "gpt-4o",
        "request_count": 3,
        "prompt_tokens": 30,
        "completion_tokens": 15,
        "total_tokens": 45,
        "cost_usd": Decimal("0.0012"),
    }
]
_KEY_ROWS = [
    {
        "api_key_prefix": "abcd1234",
        "request_count": 3,
        "prompt_tokens": 30,
        "completion_tokens": 15,
        "total_tokens": 45,
        "cost_usd": Decimal("0.0012"),
    }
]
_STATUS_ROWS = [
    {"status": "allowed", "request_count": 2},
    {"status": "blocked", "request_count": 1},
]


def _conn(pool):
    return pool.acquire.return_value.__aenter__.return_value


def _fetch_side_effect(model_rows=None, key_rows=None, status_rows=None):
    async def _fetch(query, *args, **kwargs):
        if "GROUP BY model_id" in query:
            return model_rows or []
        if "GROUP BY api_key_prefix" in query:
            return key_rows or []
        if "GROUP BY status" in query:
            return status_rows or []
        return []

    return _fetch


@pytest.fixture
async def keyed_client(gov_mock):
    """Non-admin caller with a real api_key_prefix, for AC6."""
    pool = _mock_pool()
    caller = CallerContext(
        user_id="test-user",
        tenant_id="test-tenant",
        roles=["user"],
        api_key_prefix="abcd1234",
    )
    app.dependency_overrides[get_dashboard_caller] = lambda: caller
    _setup_app_state(pool, gov_mock)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, pool

    app.dependency_overrides.clear()


async def test_dashboard_data_grouped_by_model(admin_client):
    """AC1: usage totals grouped by model are returned for the selected range."""
    client, _ = admin_client
    conn = _conn(app.state.db_pool)
    conn.fetch.side_effect = _fetch_side_effect(model_rows=_MODEL_ROWS)

    response = await client.get("/dashboard/data?range=today")

    assert response.status_code == 200
    assert "gpt-4o" in response.text
    assert "0.001200" in response.text


async def test_dashboard_data_grouped_by_api_key(admin_client):
    """AC2: usage totals grouped by API key are returned for the selected range."""
    client, _ = admin_client
    conn = _conn(app.state.db_pool)
    conn.fetch.side_effect = _fetch_side_effect(key_rows=_KEY_ROWS)

    response = await client.get("/dashboard/data?range=today")

    assert response.status_code == 200
    assert "abcd1234" in response.text


async def test_dashboard_data_supports_all_ranges(admin_client):
    """AC3: today, 7d, 30d, all, and a valid custom range are all accepted."""
    client, _ = admin_client
    conn = _conn(app.state.db_pool)
    conn.fetch.side_effect = _fetch_side_effect()

    for range_param in ("today", "7d", "30d", "all"):
        response = await client.get(f"/dashboard/data?range={range_param}")
        assert response.status_code == 200, range_param

    response = await client.get(
        "/dashboard/data?range=custom&start=2026-01-01&end=2026-01-31"
    )
    assert response.status_code == 200


async def test_custom_range_start_after_end_returns_400(async_client):
    """AC4: a custom range with start after end returns 400 with an explicit message."""
    client, _ = async_client

    response = await client.get(
        "/dashboard/data?range=custom&start=2026-02-01&end=2026-01-01"
    )

    assert response.status_code == 400
    body = response.json()
    assert body["detail"]["error"]["message"]


async def test_admin_sees_every_key_in_tenant(admin_client):
    """AC5: an admin's query filters by tenant only, not by any single API key."""
    client, _ = admin_client
    conn = _conn(app.state.db_pool)
    conn.fetch.side_effect = _fetch_side_effect(model_rows=_MODEL_ROWS)

    response = await client.get("/dashboard/data?range=today")

    assert response.status_code == 200
    for call in conn.fetch.call_args_list:
        query, *params = call.args
        assert "api_key_prefix =" not in query
        assert params[0] == "test-tenant"


async def test_non_admin_ignores_spoofed_api_key_filter(keyed_client):
    """AC6: a non-admin caller always sees only their own key, even if another
    key's prefix is passed as a query filter."""
    client, pool = keyed_client
    conn = _conn(pool)
    conn.fetch.side_effect = _fetch_side_effect(model_rows=_MODEL_ROWS)

    response = await client.get("/dashboard/data?range=today&api_key_prefix=someone-elses-key")

    assert response.status_code == 200
    assert conn.fetch.call_args_list
    for call in conn.fetch.call_args_list:
        query, *params = call.args
        assert "api_key_prefix =" in query
        assert params[-1] == "abcd1234"
        assert "someone-elses-key" not in params


async def test_no_route_leaks_other_tenant_rows(admin_client):
    """AC7: the tenant filter is always bound to the caller's own tenant_id,
    regardless of any other query params supplied."""
    client, _ = admin_client
    conn = _conn(app.state.db_pool)
    conn.fetch.side_effect = _fetch_side_effect(model_rows=_MODEL_ROWS)

    response = await client.get("/dashboard/data?range=today&tenant_id=some-other-tenant")

    assert response.status_code == 200
    assert conn.fetch.call_args_list
    for call in conn.fetch.call_args_list:
        _query, *params = call.args
        assert params[0] == "test-tenant"


async def test_unauthenticated_request_returns_401(auth_client):
    """AC8: a request with no credentials is rejected with 401."""
    client, _ = auth_client

    response = await client.get("/dashboard/data?range=today")

    assert response.status_code == 401


async def test_empty_range_renders_explicit_empty_state(admin_client):
    """AC9: a range with zero usage_log rows renders an explicit empty state."""
    client, _ = admin_client
    conn = _conn(app.state.db_pool)
    conn.fetch.side_effect = _fetch_side_effect()

    response = await client.get("/dashboard/data?range=today")

    assert response.status_code == 200
    assert "No usage in this range." in response.text


async def test_status_breakdown_shown_alongside_totals(admin_client):
    """AC10: the allowed/blocked/errored breakdown is shown with the totals."""
    client, _ = admin_client
    conn = _conn(app.state.db_pool)
    conn.fetch.side_effect = _fetch_side_effect(
        model_rows=_MODEL_ROWS, status_rows=_STATUS_ROWS
    )

    response = await client.get("/dashboard/data?range=today")

    assert response.status_code == 200
    assert "allowed" in response.text
    assert "blocked" in response.text


async def test_range_control_wired_for_htmx_partial_swap(admin_client):
    """AC11: the range control swaps the totals via htmx, not a full page navigation."""
    client, _ = admin_client
    conn = _conn(app.state.db_pool)
    conn.fetch.side_effect = _fetch_side_effect()

    page = await client.get("/dashboard")
    assert page.status_code == 200
    assert 'hx-get="/dashboard/data"' in page.text
    assert 'hx-target="#dashboard-data"' in page.text
    assert 'hx-trigger="change"' in page.text

    fragment = await client.get("/dashboard/data?range=today")
    assert fragment.status_code == 200
    assert "<html" not in fragment.text.lower()
