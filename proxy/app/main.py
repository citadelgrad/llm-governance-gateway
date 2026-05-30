from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import asyncpg
import bcrypt
import httpx
from cachetools import TTLCache
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from proxy.app.auth import AuthError, CallerContext, authenticate
from proxy.app.bootstrap import maybe_bootstrap
from proxy.app.config import settings
from proxy.app.governance_client import GovernanceError, InspectRequest, make_governance_client
from proxy.app.headers import error_envelope, pii_headers, rate_limit_headers, retry_headers
from proxy.app.providers import anthropic as anthropic_provider
from proxy.app.providers import gemini as gemini_provider
from proxy.app.providers import generic as generic_provider
from proxy.app.providers import mock as mock_provider
from proxy.app.providers import ollama as ollama_provider
from proxy.app.providers import openai as openai_provider
from proxy.app.rate_limit import RateLimiter
from proxy.app.routing import load_models_yaml, resolve_provider
from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

_tenant_cache: TTLCache = TTLCache(maxsize=500, ttl=30)
_me_cache: TTLCache = TTLCache(maxsize=500, ttl=30)

MAX_BODY_SIZE = 1 * 1024 * 1024


def _asyncpg_dsn(url: str) -> str:
    """Strip SQLAlchemy dialect prefix so asyncpg.create_pool accepts the DSN."""
    return url.replace("postgresql+asyncpg://", "postgresql://")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_pool = await asyncpg.create_pool(_asyncpg_dsn(settings.database_url))
    redis = Redis.from_url(settings.redis_url)
    rate_limiter = RateLimiter(
        redis, settings.rate_limit_requests, settings.rate_limit_window_seconds
    )
    await rate_limiter.load_script()

    gov_http = httpx.AsyncClient(timeout=10.0)
    governance_client = make_governance_client(gov_http)

    openai_client: httpx.AsyncClient | None = None
    anthropic_client: httpx.AsyncClient | None = None
    gemini_client: httpx.AsyncClient | None = None
    ollama_client: httpx.AsyncClient | None = None
    generic_client: httpx.AsyncClient | None = None
    if not settings.mock_mode:
        openai_client = openai_provider.make_client(settings.openai_api_key)
        anthropic_client = anthropic_provider.make_client(settings.anthropic_api_key)
        gemini_client = gemini_provider.make_client(settings.gemini_api_key)
        ollama_client = ollama_provider.make_client(settings.ollama_base_url)
        generic_client = generic_provider.make_client()

    await maybe_bootstrap(db_pool)

    models_config = load_models_yaml(settings.models_yaml)
    models_by_id = {m["id"]: m for m in models_config if "id" in m}

    app.state.db_pool = db_pool
    app.state.redis = redis
    app.state.rate_limiter = rate_limiter
    app.state.gov_http = gov_http
    app.state.governance_client = governance_client
    app.state.openai_client = openai_client
    app.state.anthropic_client = anthropic_client
    app.state.gemini_client = gemini_client
    app.state.ollama_client = ollama_client
    app.state.generic_client = generic_client
    app.state.models_config = models_config
    app.state.models_by_id = models_by_id
    app.state.ready = True

    yield

    app.state.ready = False
    await db_pool.close()
    await redis.aclose()
    await gov_http.aclose()
    for client in (openai_client, anthropic_client, gemini_client, ollama_client, generic_client):
        if client is not None:
            await client.aclose()


docs_url = "/docs" if settings.docs_enabled else None
redoc_url = "/redoc" if settings.docs_enabled else None
app = FastAPI(title="AI Gateway Proxy", docs_url=docs_url, redoc_url=redoc_url, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_BODY_SIZE:
            return Response(content="Request body too large (max 1MB)", status_code=413)
        return await call_next(request)


app.add_middleware(BodySizeLimitMiddleware)


async def get_caller(
    authorization: str | None = Header(default=None),
    request: Request = ...,
) -> CallerContext:
    try:
        return await authenticate(authorization, request.app.state.db_pool)
    except AuthError as exc:
        raise HTTPException(
            status_code=401, detail=error_envelope("auth_error", "Invalid credentials")
        ) from exc


async def get_tenant_info(tenant_id: str, db_pool: asyncpg.Pool) -> dict:
    if tenant_id in _tenant_cache:
        return _tenant_cache[tenant_id]

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT default_provider, allowed_models, pii_redaction_notification,"
            " rate_limit_requests_per_minute FROM tenants WHERE tenant_id = $1",
            tenant_id,
        )

    if row is None:
        result = {
            "default_provider": "",
            "allowed_models": [],
            "pii_redaction_notification": "header",
            "rate_limit_requests_per_minute": settings.rate_limit_requests,
        }
    else:
        result = {
            "default_provider": row["default_provider"] or "",
            "allowed_models": list(row["allowed_models"] or []),
            "pii_redaction_notification": row["pii_redaction_notification"] or "header",
            "rate_limit_requests_per_minute": row["rate_limit_requests_per_minute"]
            or settings.rate_limit_requests,
        }

    _tenant_cache[tenant_id] = result
    return result


def _extract_user_message(body: dict) -> str:
    messages = body.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
    return ""


@app.get("/health")
async def health():
    if not app.state.ready:
        return Response(status_code=503)
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    caller: CallerContext = Depends(get_caller),
):
    body = await request.json()
    model_id = body.get("model", "")
    stream = body.get("stream", False)

    tenant = await get_tenant_info(caller.tenant_id, request.app.state.db_pool)

    lower_headers = {k.lower(): v for k, v in request.headers.items()}
    provider, routing_method = resolve_provider(
        model_id,
        lower_headers,
        caller.roles,
        tenant["default_provider"],
        request.app.state.models_config,
    )

    if routing_method in ("model_not_found", "override_denied"):
        raise HTTPException(
            status_code=400,
            detail=error_envelope(routing_method, f"Cannot route model: {model_id}"),
        )

    rl_result = await request.app.state.rate_limiter.check(f"{caller.tenant_id}:{caller.user_id}")
    reset_at = datetime.now(UTC) + timedelta(seconds=settings.rate_limit_window_seconds)
    rl_hdrs = rate_limit_headers(rl_result.limit, rl_result.remaining, reset_at)

    if not rl_result.allowed:
        raise HTTPException(
            status_code=429,
            detail=error_envelope("rate_limit_exceeded", "Too many requests"),
            headers={**retry_headers(rl_result.retry_after_seconds), **rl_hdrs},
        )

    user_text = _extract_user_message(body)
    try:
        inspect_resp = await request.app.state.governance_client.inspect(
            InspectRequest(
                text=user_text,
                tenant_id=caller.tenant_id,
                user_id=caller.user_id,
                model_id=model_id,
                routing_method=routing_method,
                roles=caller.roles,
            )
        )
    except GovernanceError as exc:
        raise HTTPException(
            status_code=503,
            detail=error_envelope("governance_unavailable", "Governance service unavailable"),
        ) from exc

    extra_headers: dict[str, str] = {**rl_hdrs}
    if inspect_resp.audit_id:
        extra_headers["X-Audit-ID"] = inspect_resp.audit_id

    if inspect_resp.decision == "block":
        block_status = 400 if any(v == "harm:prompt_injection" for v in inspect_resp.violations) else 403
        return JSONResponse(
            content=error_envelope(
                "policy_violation",
                "Request blocked by policy",
                violations=inspect_resp.violations,
            ),
            status_code=block_status,
            headers=extra_headers,
        )

    if inspect_resp.pii_findings:
        pii_types = [f.get("type", "") for f in inspect_resp.pii_findings if isinstance(f, dict)]
        extra_headers.update(
            pii_headers(pii_types, tenant["pii_redaction_notification"])
        )
        if inspect_resp.redacted_text:
            messages = body.get("messages", [])
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    msg["content"] = inspect_resp.redacted_text
                    break

    match provider:
        case _ if settings.mock_mode:
            return await mock_provider.chat_completions(body, extra_headers)
        case "openai":
            return await openai_provider.chat_completions(
                request.app.state.openai_client, body, stream, extra_headers
            )
        case "anthropic":
            return await anthropic_provider.chat_completions(
                request.app.state.anthropic_client, body, stream, extra_headers
            )
        case "gemini" | "google":
            return await gemini_provider.chat_completions(
                request.app.state.gemini_client, body, stream, extra_headers
            )
        case "ollama":
            return await ollama_provider.chat_completions(
                request.app.state.ollama_client, body, stream, extra_headers
            )
        case _:
            model_entry = request.app.state.models_by_id.get(model_id)
            if model_entry and model_entry.get("base_url"):
                return await generic_provider.chat_completions(
                    request.app.state.generic_client,
                    body,
                    stream,
                    extra_headers,
                    base_url=model_entry["base_url"],
                    api_key=model_entry.get("api_key", ""),
                )
            raise HTTPException(
                status_code=400,
                detail=error_envelope("unsupported_provider", f"Provider {provider} not supported"),
            )


@app.get("/v1/models")
async def list_models(
    request: Request,
    caller: CallerContext = Depends(get_caller),
):
    tenant = await get_tenant_info(caller.tenant_id, request.app.state.db_pool)
    allowed = set(tenant["allowed_models"])
    models = request.app.state.models_config

    if allowed:
        data = [
            {"id": m["id"], "object": "model", "created": 0, "owned_by": m["provider"]}
            for m in models
            if m["id"] in allowed
        ]
    else:
        data = [
            {"id": m["id"], "object": "model", "created": 0, "owned_by": m["provider"]}
            for m in models
        ]

    return {"object": "list", "data": data}


@app.get("/v1/me")
async def me(
    request: Request,
    caller: CallerContext = Depends(get_caller),
):
    cache_key = caller.user_id
    if cache_key in _me_cache:
        return _me_cache[cache_key]

    tenant = await get_tenant_info(caller.tenant_id, request.app.state.db_pool)
    reset_at = datetime.now(UTC) + timedelta(seconds=settings.rate_limit_window_seconds)

    result = {
        "user_id": caller.user_id,
        "tenant_id": caller.tenant_id,
        "roles": caller.roles,
        "allowed_models": tenant["allowed_models"],
        "rate_limit": {
            "requests_per_minute": tenant["rate_limit_requests_per_minute"],
            "resets_at": reset_at.isoformat(),
        },
        "pii_policy": {"notification": tenant["pii_redaction_notification"]},
    }
    _me_cache[cache_key] = result
    return result


@app.post("/v1/keys")
async def create_key(
    request: Request,
    caller: CallerContext = Depends(get_caller),
):
    if "admin" not in caller.roles:
        raise HTTPException(
            status_code=403,
            detail=error_envelope("forbidden", "Admin role required"),
        )

    payload = await request.json()
    user_id: str = payload["user_id"]
    tenant_id: str = payload["tenant_id"]
    roles: list[str] = payload.get("roles", [])

    key = secrets.token_urlsafe(32)
    prefix = key[:8]
    key_hash = bcrypt.hashpw(key.encode(), bcrypt.gensalt()).decode()

    async with request.app.state.db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO api_keys(prefix, hash, user_id, tenant_id, roles)"
            " VALUES($1, $2, $3, $4, $5)",
            prefix,
            key_hash,
            user_id,
            tenant_id,
            roles,
        )

    return {"key": key}


@app.delete("/v1/users/{user_id}", status_code=202)
async def delete_user(
    user_id: str,
    request: Request,
    caller: CallerContext = Depends(get_caller),
):
    if "admin" not in caller.roles:
        raise HTTPException(
            status_code=403,
            detail=error_envelope("forbidden", "Admin role required"),
        )

    resp = await request.app.state.gov_http.delete(
        f"{settings.governance_url}/v1/users/{user_id}",
        params={"tenant_id": caller.tenant_id},
        headers={"X-Internal-Token": settings.governance_internal_token},
    )

    return Response(
        content=resp.content,
        status_code=202,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@app.get("/v1/audit")
async def audit(
    request: Request,
    caller: CallerContext = Depends(get_caller),
):
    if "admin" not in caller.roles:
        raise HTTPException(
            status_code=403,
            detail=error_envelope("forbidden", "Admin role required"),
        )

    resp = await request.app.state.gov_http.get(
        f"{settings.governance_url}/v1/audit",
        headers={"X-Internal-Token": settings.governance_internal_token},
        params=dict(request.query_params),
    )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@app.get("/v1/audit/export")
async def audit_export(
    request: Request,
    caller: CallerContext = Depends(get_caller),
):
    if "admin" not in caller.roles:
        raise HTTPException(
            status_code=403,
            detail=error_envelope("forbidden", "Admin role required"),
        )

    async def _stream():
        async with request.app.state.gov_http.stream(
            "GET",
            f"{settings.governance_url}/v1/audit/export",
            headers={"X-Internal-Token": settings.governance_internal_token},
            params=dict(request.query_params),
        ) as upstream:
            async for chunk in upstream.aiter_bytes():
                yield chunk

    return StreamingResponse(_stream(), media_type="application/octet-stream")
