from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from proxy.app.auth import AuthError, CallerContext, authenticate
from proxy.app.headers import error_envelope
from starlette.templating import Jinja2Templates

router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_VALID_RANGES = {"today", "7d", "30d", "all", "custom"}


async def get_dashboard_caller(
    request: Request,
    authorization: str | None = Header(default=None),
) -> CallerContext:
    """Auth dependency for the dashboard routes.

    Mirrors main.py's get_caller; duplicated rather than imported to avoid a
    circular import (main.py mounts this router).
    """
    try:
        return await authenticate(
            authorization,
            request.app.state.db_pool,
            allow_bearer_api_key_fallback=True,
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=401, detail=error_envelope("auth_error", "Invalid credentials")
        ) from exc


@dataclass(frozen=True)
class DashboardScope:
    tenant_id: str
    api_key_prefix: str | None  # None = no filter (admin sees the whole tenant)


def resolve_scope(caller: CallerContext) -> DashboardScope | None:
    """Resolve the visibility scope for a dashboard request.

    Admins see every row in their tenant. Non-admins see only their own API
    key's rows, and any api_key_prefix query param they pass is ignored
    (AC6). A non-admin caller with no real API key (JWT auth, no
    api_key_prefix) has nothing it is safe to show and gets no visible rows,
    rather than matching every other JWT caller's rows on an empty string.
    """
    if "admin" in caller.roles:
        return DashboardScope(tenant_id=caller.tenant_id, api_key_prefix=None)
    if not caller.api_key_prefix:
        return None
    return DashboardScope(tenant_id=caller.tenant_id, api_key_prefix=caller.api_key_prefix)


def resolve_range(
    range_param: str,
    start_param: str | None,
    end_param: str | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    """Resolve range query params to a concrete [start, end] datetime window.

    Raises HTTPException(400) for an unknown range or an invalid custom range.
    """
    if range_param not in _VALID_RANGES:
        raise HTTPException(
            status_code=400,
            detail=error_envelope("invalid_range", f"Unknown range: {range_param}"),
        )

    if range_param == "today":
        return datetime.combine(now.date(), time.min, tzinfo=UTC), now
    if range_param == "7d":
        return now - timedelta(days=7), now
    if range_param == "30d":
        return now - timedelta(days=30), now
    if range_param == "all":
        return _EPOCH, now

    if not start_param or not end_param:
        raise HTTPException(
            status_code=400,
            detail=error_envelope(
                "invalid_range", "Custom range requires both start and end dates"
            ),
        )
    try:
        start_date = date.fromisoformat(start_param)
        end_date = date.fromisoformat(end_param)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=error_envelope(
                "invalid_range", "start and end must be ISO dates (YYYY-MM-DD)"
            ),
        ) from exc

    if start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail=error_envelope("invalid_range", "start date must not be after end date"),
        )

    return (
        datetime.combine(start_date, time.min, tzinfo=UTC),
        datetime.combine(end_date, time.max, tzinfo=UTC),
    )


def _scope_where(scope: DashboardScope, start: datetime, end: datetime) -> tuple[str, list]:
    params: list = [scope.tenant_id, start, end]
    where = "tenant_id = $1 AND created_at >= $2 AND created_at <= $3"
    if scope.api_key_prefix is not None:
        params.append(scope.api_key_prefix)
        where += f" AND api_key_prefix = ${len(params)}"
    return where, params


async def fetch_by_model(
    pool: asyncpg.Pool, scope: DashboardScope, start: datetime, end: datetime
) -> list[asyncpg.Record]:
    where, params = _scope_where(scope, start, end)
    query = (
        "SELECT model_id, COUNT(*) AS request_count, "
        "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
        "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
        "SUM(cost_usd) AS cost_usd "
        f"FROM usage_log WHERE {where} GROUP BY model_id ORDER BY model_id"
    )
    async with pool.acquire() as conn:
        return await conn.fetch(query, *params)


async def fetch_by_api_key(
    pool: asyncpg.Pool, scope: DashboardScope, start: datetime, end: datetime
) -> list[asyncpg.Record]:
    where, params = _scope_where(scope, start, end)
    query = (
        "SELECT api_key_prefix, COUNT(*) AS request_count, "
        "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
        "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
        "SUM(cost_usd) AS cost_usd "
        f"FROM usage_log WHERE {where} GROUP BY api_key_prefix ORDER BY api_key_prefix"
    )
    async with pool.acquire() as conn:
        return await conn.fetch(query, *params)


async def fetch_status_breakdown(
    pool: asyncpg.Pool, scope: DashboardScope, start: datetime, end: datetime
) -> list[asyncpg.Record]:
    where, params = _scope_where(scope, start, end)
    query = (
        "SELECT status, COUNT(*) AS request_count "
        f"FROM usage_log WHERE {where} GROUP BY status ORDER BY status"
    )
    async with pool.acquire() as conn:
        return await conn.fetch(query, *params)


@router.get("/dashboard")
async def dashboard_page(
    request: Request,
    caller: CallerContext = Depends(get_dashboard_caller),
):
    return templates.TemplateResponse(request, "dashboard.html", {"caller": caller})


@router.get("/dashboard/data")
async def dashboard_data(
    request: Request,
    range: str = "today",
    start: str | None = None,
    end: str | None = None,
    api_key_prefix: str | None = None,
    caller: CallerContext = Depends(get_dashboard_caller),
):
    range_start, range_end = resolve_range(range, start, end, datetime.now(UTC))
    scope = resolve_scope(caller)

    if scope is None:
        by_model: list[asyncpg.Record] = []
        by_api_key: list[asyncpg.Record] = []
        status_breakdown: list[asyncpg.Record] = []
    else:
        if "admin" in caller.roles and api_key_prefix:
            scope = DashboardScope(tenant_id=scope.tenant_id, api_key_prefix=api_key_prefix)
        pool = request.app.state.db_pool
        by_model = await fetch_by_model(pool, scope, range_start, range_end)
        by_api_key = await fetch_by_api_key(pool, scope, range_start, range_end)
        status_breakdown = await fetch_status_breakdown(pool, scope, range_start, range_end)

    has_usage = bool(by_model or by_api_key or status_breakdown)

    return templates.TemplateResponse(
        request,
        "_dashboard_data.html",
        {
            "by_model": by_model,
            "by_api_key": by_api_key,
            "status_breakdown": status_breakdown,
            "has_usage": has_usage,
            "range": range,
            "start": start,
            "end": end,
        },
    )
