from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .context import PipelineContext
from . import pseudonym as pseudonym_module


def uuid7() -> UUID:
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & 0x3FFFFFFFFFFFFFFF
    hi = (ms << 16) | (0x7 << 12) | rand_a
    lo = 0x8000000000000000 | rand_b
    return UUID(int=(hi << 64) | lo)


async def write_audit(
    session: AsyncSession,
    ctx: PipelineContext,
    hmac_key: str,
    audit_id: str,
) -> None:
    """Write audit record. audit_id is provided by the caller for response correlation."""
    now = datetime.now(timezone.utc)

    try:
        user_pseudonym = await pseudonym_module.get_or_create(
            session, hmac_key, ctx.tenant_id, ctx.user_id
        )

        await session.execute(
            text("""
                INSERT INTO audit_log (
                    audit_id, created_at, written_at,
                    user_id, tenant_id, model_id, routing_method,
                    decision, pii_findings, harm_score, violations, phase
                ) VALUES (
                    :audit_id, :created_at, :written_at,
                    :user_id, :tenant_id, :model_id, :routing_method,
                    :decision, :pii_findings::jsonb, :harm_score, :violations::jsonb, :phase
                )
            """),
            {
                "audit_id": audit_id,
                "created_at": now,
                "written_at": datetime.now(timezone.utc),  # populated after processing
                "user_id": user_pseudonym,
                "tenant_id": ctx.tenant_id,
                "model_id": ctx.model_id,
                "routing_method": ctx.routing_method,
                "decision": ctx.decision,
                "pii_findings": json.dumps(ctx.pii_findings),  # [{type,start,end,score}] only
                "harm_score": ctx.harm_score,
                "violations": json.dumps(ctx.violations),
                "phase": ctx.phase,
            },
        )
        await session.commit()
    except Exception as exc:
        print(f"[audit] write_audit failed: {exc}", file=sys.stderr)
        await session.rollback()
