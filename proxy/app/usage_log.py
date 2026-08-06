from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

import asyncpg
from proxy.app.providers.usage import UsageMetrics

logger = logging.getLogger(__name__)


async def resolve_cost(
    pool: asyncpg.Pool,
    model_id: str,
    usage: UsageMetrics,
    at: datetime,
) -> Decimal | None:
    """Look up the pricing rate effective at `at` and compute cost.

    Returns None if no pricing row is effective for model_id at that time.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT input_rate_usd_per_token, output_rate_usd_per_token "
            "FROM pricing WHERE model_id = $1 AND effective_from <= $2 "
            "ORDER BY effective_from DESC LIMIT 1",
            model_id,
            at.date(),
        )
    if row is None:
        return None
    return (
        row["input_rate_usd_per_token"] * usage.prompt_tokens
        + row["output_rate_usd_per_token"] * usage.completion_tokens
    )


async def write_usage_log(
    pool: asyncpg.Pool,
    *,
    created_at: datetime,
    tenant_id: str,
    api_key_prefix: str | None,
    user_id: str,
    model_id: str,
    status: str,
    usage: UsageMetrics,
    cost_usd: Decimal | None,
    latency_ms: int,
) -> None:
    """Insert one usage_log row. Never raises — a logging failure must not
    affect the client-facing request (AC8).
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO usage_log (created_at, tenant_id, api_key_prefix, "
                "user_id, model_id, status, prompt_tokens, completion_tokens, "
                "total_tokens, cost_usd, latency_ms) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                created_at,
                tenant_id,
                api_key_prefix or "",
                user_id,
                model_id,
                status,
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.total_tokens,
                cost_usd,
                latency_ms,
            )
    except Exception:
        logger.exception("Failed to write usage_log row")
