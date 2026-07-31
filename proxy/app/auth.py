from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import asyncpg
import bcrypt
from cachetools import TTLCache
from jose import JWTError, jwt
from proxy.app.config import settings


class AuthError(Exception):
    pass


@dataclass
class CallerContext:
    user_id: str
    tenant_id: str
    roles: list[str]
    scopes: list[str] = field(default_factory=list)
    client_id: str | None = None
    act_sub: str | None = None


_api_key_cache: TTLCache = TTLCache(maxsize=1000, ttl=60)


async def _validate_api_key(key: str, db_pool: asyncpg.Pool) -> CallerContext:
    prefix = key[:8]
    cache_key = (prefix, key[:16])

    if cache_key in _api_key_cache:
        cached = _api_key_cache[cache_key]
        valid = await asyncio.get_event_loop().run_in_executor(
            None, bcrypt.checkpw, key.encode(), cached["hash"].encode()
        )
        if valid:
            return CallerContext(
                user_id=cached["user_id"],
                tenant_id=cached["tenant_id"],
                roles=cached["roles"],
            )
        raise AuthError("invalid api key")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT hash, user_id, tenant_id, roles FROM api_keys WHERE prefix = $1",
            prefix,
        )

    if row is None:
        raise AuthError("invalid api key")

    valid = await asyncio.get_event_loop().run_in_executor(
        None, bcrypt.checkpw, key.encode(), row["hash"].encode()
    )
    if not valid:
        raise AuthError("invalid api key")

    _api_key_cache[cache_key] = {
        "hash": row["hash"],
        "user_id": row["user_id"],
        "tenant_id": row["tenant_id"],
        "roles": list(row["roles"]),
    }

    return CallerContext(
        user_id=row["user_id"],
        tenant_id=row["tenant_id"],
        roles=list(row["roles"]),
    )


def _validate_jwt(token: str) -> CallerContext:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except JWTError as exc:
        raise AuthError(f"invalid token: {exc}") from exc

    user_id = claims.get("user_id")
    tenant_id = claims.get("tenant_id")
    roles = claims.get("roles", [])

    if not user_id or not tenant_id:
        raise AuthError("token missing required claims")

    scope_claim = claims.get("scope", claims.get("scopes", []))
    scopes = scope_claim.split() if isinstance(scope_claim, str) else list(scope_claim or [])
    client_id = claims.get("client_id")
    act_claim = claims.get("act") or {}
    act_sub = act_claim.get("sub") if isinstance(act_claim, dict) else None

    return CallerContext(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=roles,
        scopes=scopes,
        client_id=client_id,
        act_sub=act_sub,
    )


async def authenticate(
    authorization: str | None,
    db_pool: asyncpg.Pool,
    *,
    allow_bearer_api_key_fallback: bool = False,
) -> CallerContext:
    """Raises AuthError if invalid."""
    if not authorization:
        raise AuthError("missing credentials")

    if authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
        try:
            return _validate_jwt(token)
        except AuthError:
            if allow_bearer_api_key_fallback:
                return await _validate_api_key(token, db_pool)
            raise

    if authorization.startswith("ApiKey "):
        key = authorization[len("ApiKey "):]
        return await _validate_api_key(key, db_pool)

    # Treat bare value as an API key
    if " " not in authorization:
        return await _validate_api_key(authorization, db_pool)

    raise AuthError("missing credentials")


async def authenticate_compat(
    authorization: str | None,
    x_api_key: str | None,
    db_pool: asyncpg.Pool,
) -> CallerContext:
    """Compat auth for Anthropic-compatible endpoints.

    Accepts x-api-key and API keys carried as Authorization: Bearer for Claude
    Code/Anthropic SDK compatibility. Bearer JWTs still work, so shared routes
    such as /v1/models do not regress existing clients.
    """
    if x_api_key:
        return await _validate_api_key(x_api_key, db_pool)
    if not authorization:
        raise AuthError("missing credentials")
    if authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
        try:
            return _validate_jwt(token)
        except AuthError:
            return await _validate_api_key(token, db_pool)
    if authorization.startswith("ApiKey "):
        return await _validate_api_key(authorization[len("ApiKey "):], db_pool)
    if " " not in authorization:
        return await _validate_api_key(authorization, db_pool)
    raise AuthError("missing credentials")
