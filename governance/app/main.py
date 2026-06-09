import asyncio
import json
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response as StarletteResponse

from . import audit as audit_module
from . import pipeline as pipeline_module
from . import retention
from .context import PipelineContext
from .db import get_session, get_session_factory
from .settings import settings

_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ready
    from . import harm as harm_module
    from . import opa as opa_module
    from . import pii
    await asyncio.to_thread(harm_module.warm_up_scanners)  # warm up harm scanners at startup
    await pii.initialize(settings.spacy_model)
    _ready = True
    yield
    _ready = False
    await opa_module.close_client()


app = FastAPI(title="AI Gateway Governance", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

MAX_BODY_SIZE = 1 * 1024 * 1024  # 1MB


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_BODY_SIZE:
            return StarletteResponse(
                content="Request body too large (max 1MB)",
                status_code=413,
            )
        return await call_next(request)


app.add_middleware(BodySizeLimitMiddleware)


@app.get("/health")
async def health():
    if not _ready:
        return Response(status_code=503, content="starting")
    try:
        factory = get_session_factory()
        async with factory() as session:
            stuck = await retention.count_stuck_partitions(session)
            if stuck > 0:
                return {"status": "degraded", "reason": f"{stuck} stuck partition(s)"}
    except Exception:
        pass
    return {"status": "ok"}


# ─── /inspect ────────────────────────────────────────────────────────────────

class InspectRequest(BaseModel):
    text: str
    tenant_id: str
    user_id: str
    model_id: str
    routing_method: str
    phase: str = "pre_call"
    roles: list[str] = []


class InspectResponse(BaseModel):
    decision: str               # "allow" | "block"
    redacted_text: str
    pii_findings: list[dict]    # [{type, start, end, score}] only
    harm_score: float
    violations: list[str]
    audit_id: str | None        # eventually consistent via BackgroundTasks


@app.post("/inspect", response_model=InspectResponse)
async def inspect(
    req: InspectRequest,
    background_tasks: BackgroundTasks,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> InspectResponse:
    if not x_internal_token or not secrets.compare_digest(x_internal_token, settings.internal_token):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Internal-Token")

    ctx = PipelineContext(
        text=req.text,
        tenant_id=req.tenant_id,
        user_id=req.user_id,
        model_id=req.model_id,
        routing_method=req.routing_method,
        phase=req.phase,
        roles=req.roles,
    )

    request_received_at = datetime.now(UTC)
    await pipeline_module.run(ctx, settings.opa_url)

    audit_id = str(audit_module.uuid7())
    ctx.audit_id = audit_id

    async def _do_audit():
        try:
            factory = get_session_factory()
            async with factory() as new_session:
                await audit_module.write_audit(new_session, ctx, settings.pseudonym_hmac_key, audit_id, request_received_at)
        except Exception as exc:
            print(f"[inspect] background audit {audit_id} failed: {exc}", file=sys.stderr)

    background_tasks.add_task(_do_audit)

    return InspectResponse(
        decision=ctx.decision,
        redacted_text=ctx.redacted_text or ctx.text,
        pii_findings=ctx.pii_findings,
        harm_score=ctx.harm_score,
        violations=ctx.violations,
        audit_id=audit_id,
    )


# ─── /v1/audit/export ────────────────────────────────────────────────────────

@app.get("/v1/audit/export")
async def audit_export(
    after_created_at: datetime | None = Query(default=None),
    after_audit_id: str | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=500, le=1000),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    if not x_internal_token or not secrets.compare_digest(x_internal_token, settings.internal_token):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Internal-Token")

    if until is None:
        until = datetime.now(UTC)

    if after_created_at is None or after_audit_id is None:
        # First page — no keyset
        result = await session.execute(
            text("""
                SELECT audit_id, created_at, written_at, user_id, tenant_id,
                       model_id, routing_method, decision, pii_findings,
                       harm_score, violations, phase
                FROM audit_log
                WHERE created_at <= :until
                ORDER BY created_at, audit_id
                LIMIT :limit
            """),
            {"until": until, "limit": limit},
        )
    else:
        result = await session.execute(
            text("""
                SELECT audit_id, created_at, written_at, user_id, tenant_id,
                       model_id, routing_method, decision, pii_findings,
                       harm_score, violations, phase
                FROM audit_log
                WHERE (created_at, audit_id) > (:after_created_at, :after_audit_id)
                  AND created_at <= :until
                ORDER BY created_at, audit_id
                LIMIT :limit
            """),
            {
                "after_created_at": after_created_at,
                "after_audit_id": after_audit_id,
                "until": until,
                "limit": limit,
            },
        )

    rows = result.fetchall()

    async def generate():
        for row in rows:
            record = {
                "audit_id": row.audit_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "written_at": row.written_at.isoformat() if row.written_at else None,
                "user_id": row.user_id,
                "tenant_id": row.tenant_id,
                "model_id": row.model_id,
                "routing_method": row.routing_method,
                "decision": row.decision,
                "pii_findings": row.pii_findings,
                "harm_score": row.harm_score,
                "violations": row.violations,
                "phase": row.phase,
            }
            yield json.dumps(record) + "\n"

    headers = {}
    if len(rows) == limit:
        last = rows[-1]
        last_created_at = last.created_at.isoformat()
        last_audit_id = last.audit_id
        next_url = (
            f"/v1/audit/export?"
            f"after_created_at={last_created_at}&after_audit_id={last_audit_id}"
            f"&until={until.isoformat()}"
        )
        headers["Link"] = f'<{next_url}>; rel="next"'

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers=headers,
    )


@app.get("/v1/audit")
async def audit_list(
    tenant_id: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    session: AsyncSession = Depends(get_session),
):
    if not x_internal_token or not secrets.compare_digest(x_internal_token, settings.internal_token):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Internal-Token")

    result = await session.execute(
        text("""
            SELECT audit_id, created_at, user_id, tenant_id, model_id,
                   routing_method, decision, harm_score, phase
            FROM audit_log
            WHERE (:tenant_id IS NULL OR tenant_id = :tenant_id)
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"tenant_id": tenant_id, "limit": limit},
    )
    rows = result.fetchall()
    return [dict(r._mapping) for r in rows]


# ─── /v1/users/{user_id} (GDPR erasure) ──────────────────────────────────────

class ErasureResponse(BaseModel):
    erasure_id: str
    pseudonyms_erased: int
    audit_row_count: int


@app.delete("/v1/users/{user_id}", status_code=202, response_model=ErasureResponse)
async def erase_user(
    user_id: str,
    tenant_id: str = Query(...),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    session: AsyncSession = Depends(get_session),
) -> ErasureResponse:
    if not x_internal_token or not secrets.compare_digest(x_internal_token, settings.internal_token):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Internal-Token")

    # Find all pseudonyms for this user+tenant across rotations
    result = await session.execute(
        text("""
            SELECT pseudonym FROM user_pseudonym_map
            WHERE real_user_id = :user_id AND tenant_id = :tenant_id AND deleted_at IS NULL
        """),
        {"user_id": user_id, "tenant_id": tenant_id},
    )
    pseudonyms = [row.pseudonym for row in result.fetchall()]

    if not pseudonyms:
        raise HTTPException(status_code=404, detail="User not found")

    # Count audit rows for this user
    audit_count_result = await session.execute(
        text("""
            SELECT COUNT(*) FROM audit_log
            WHERE user_id = ANY(:pseudonyms) AND tenant_id = :tenant_id
        """),
        {"pseudonyms": pseudonyms, "tenant_id": tenant_id},
    )
    audit_row_count = audit_count_result.scalar() or 0

    # Overwrite real_user_id with sentinel and set deleted_at
    now = datetime.now(UTC)
    await session.execute(
        text("""
            UPDATE user_pseudonym_map
            SET real_user_id = '[ERASED]', deleted_at = :now
            WHERE real_user_id = :user_id AND tenant_id = :tenant_id AND deleted_at IS NULL
        """),
        {"now": now, "user_id": user_id, "tenant_id": tenant_id},
    )

    # Write erasure log for each pseudonym
    erasure_id = str(audit_module.uuid7())
    for pseudonym in pseudonyms:
        await session.execute(
            text("""
                INSERT INTO erasure_log (erasure_id, pseudonym, audit_row_count, erased_by)
                VALUES (:erasure_id::uuid, :pseudonym, :audit_row_count, :erased_by)
            """),
            {
                "erasure_id": erasure_id,
                "pseudonym": pseudonym,
                "audit_row_count": audit_row_count,
                "erased_by": "api",
            },
        )

    await session.commit()

    return ErasureResponse(
        erasure_id=erasure_id,
        pseudonyms_erased=len(pseudonyms),
        audit_row_count=audit_row_count,
    )
