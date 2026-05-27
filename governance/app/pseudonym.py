from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _compute(hmac_key: str, tenant_id: str, real_user_id: str, rotation_id: int) -> str:
    """Deterministic HMAC-SHA256 pseudonym."""
    message = f"{tenant_id}:{real_user_id}:{rotation_id}".encode()
    return hmac.new(hmac_key.encode(), message, hashlib.sha256).hexdigest()


async def get_or_create(
    session: AsyncSession,
    hmac_key: str,
    tenant_id: str,
    real_user_id: str,
    rotation_id: int = 0,
) -> str:
    """Return pseudonym, inserting user_pseudonym_map row on first use."""
    pseudonym = _compute(hmac_key, tenant_id, real_user_id, rotation_id)

    await session.execute(
        text("""
            INSERT INTO user_pseudonym_map (pseudonym, real_user_id, tenant_id, rotation_id)
            VALUES (:pseudonym, :real_user_id, :tenant_id, :rotation_id)
            ON CONFLICT (real_user_id, tenant_id, rotation_id) DO NOTHING
        """),
        {
            "pseudonym": pseudonym,
            "real_user_id": real_user_id,
            "tenant_id": tenant_id,
            "rotation_id": rotation_id,
        },
    )
    return pseudonym
