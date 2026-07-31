from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import pseudonym as pseudonym_module
from .context import PipelineContext


def uuid7() -> UUID:
    # Task 11: fix non-standard bit extraction in hand-rolled UUID7.
    # Layout (RFC draft): 48-bit unix_ts_ms | 4-bit ver(7) | 12-bit rand_a | 2-bit var | 62-bit rand_b
    # rand is 80 random bits; rand_a takes the top 12 bits (bits 79..68), rand_b takes bits 61..0.
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits
    rand_a = (rand >> 68) & 0xFFF   # top 12 bits of 80-bit value (was incorrectly >> 62)
    rand_b = rand & 0x3FFFFFFFFFFFFFFF  # low 62 bits for rand_b field
    hi = (ms << 16) | (0x7 << 12) | rand_a
    lo = 0x8000000000000000 | rand_b
    return UUID(int=(hi << 64) | lo)


async def write_audit(
    session: AsyncSession,
    ctx: PipelineContext,
    hmac_key: str,
    audit_id: str,
    created_at: datetime | None = None,
) -> None:
    """Write audit record. audit_id is provided by the caller for response correlation.

    Task 10: created_at is the request-received time (passed by the caller) so it differs
    from written_at (the time the background DB write executes).
    """
    now = datetime.now(UTC)
    record_created_at = created_at or now

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
                    :decision,
                    CAST(:pii_findings AS jsonb),
                    :harm_score,
                    CAST(:violations AS jsonb),
                    :phase
                )
            """),
            {
                "audit_id": audit_id,
                "created_at": record_created_at,
                "written_at": now,
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


async def write_mcp_audit_event(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    hmac_key: str,
    audit_id: str,
    event_type: str,
    decision: str,
) -> None:
    """Write an audit record for an MCP tool-call event.

    MCP tool calls have no model_id/routing_method in the LLM-pipeline sense,
    so this writes routing_method="mcp" (sentinel) and model_id=NULL directly
    instead of going through PipelineContext/write_audit, which would
    misrepresent an MCP event as an LLM one. Unlike write_audit (a
    best-effort BackgroundTasks write), failures here propagate so the
    caller (the /v1/mcp/audit-event endpoint) surfaces them.
    """
    now = datetime.now(UTC)
    user_pseudonym = await pseudonym_module.get_or_create(session, hmac_key, tenant_id, user_id)

    await session.execute(
        text("""
            INSERT INTO audit_log (
                audit_id, created_at, written_at,
                user_id, tenant_id, model_id, routing_method,
                decision, violations, phase
            ) VALUES (
                :audit_id, :created_at, :written_at,
                :user_id, :tenant_id, NULL, 'mcp',
                :decision,
                CAST(:violations AS jsonb),
                :phase
            )
        """),
        {
            "audit_id": audit_id,
            "created_at": now,
            "written_at": now,
            "user_id": user_pseudonym,
            "tenant_id": tenant_id,
            "decision": decision,
            "violations": json.dumps([event_type] if decision == "block" else []),
            "phase": event_type,
        },
    )
    await session.commit()
