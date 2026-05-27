from __future__ import annotations

import os

import asyncpg
import bcrypt


async def maybe_bootstrap(pool: asyncpg.Pool) -> None:
    """
    Run at proxy startup before serving requests.
    Creates the initial admin API key if and only if:
      1. GATEWAY_BOOTSTRAP_TOKEN env var is set
      2. bootstrap_state table has no row with bootstrapped=true
    Idempotent and concurrency-safe.
    """
    token = os.environ.get("GATEWAY_BOOTSTRAP_TOKEN")
    if not token:
        return

    async with pool.acquire() as conn, conn.transaction():
        already = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM bootstrap_state WHERE bootstrapped = true)"
        )
        if already:
            return

        prefix = token[:8]
        hashed = bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()

        await conn.execute(
            "INSERT INTO api_keys(prefix, hash, user_id, tenant_id, roles)"
            " VALUES($1, $2, 'admin', 'system', ARRAY['admin', 'superuser'])"
            " ON CONFLICT DO NOTHING",
            prefix,
            hashed,
        )
        await conn.execute(
            "INSERT INTO bootstrap_state(bootstrapped) VALUES(true) ON CONFLICT DO NOTHING"
        )

    os.environ.pop("GATEWAY_BOOTSTRAP_TOKEN", None)
