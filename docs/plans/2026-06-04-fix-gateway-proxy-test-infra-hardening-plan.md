---
title: "fix: Gateway Proxy and Test Infrastructure Hardening"
type: fix
status: active
date: 2026-06-04
deepened: 2026-06-04
issues: [gw-11d, gw-m2n, gw-2x9, gw-3bi, gw-twv, gw-8ph, gw-mbl, gw-40f, gw-p7n, gw-b90, gw-wsx, gw-s20, gw-ynk, gw-3mh, gw-cev, gw-1ex]
---

# fix: Gateway Proxy and Test Infrastructure Hardening

## Enhancement Summary

**Deepened:** 2026-06-04
**Research agents:** kieran-python-reviewer, architecture-strategist, security-sentinel, performance-oracle, code-simplicity-reviewer, data-integrity-guardian, deployment-verification-agent, best-practices-researcher (hypothesis+asyncio), framework-docs-researcher (JWT/asyncpg/pydantic), property-based-testing skill, learnings (fastapi-rate-limiting, cors-security), pattern-recognition-specialist

### Critical Corrections Found During Research

1. **gw-40f is likely already fixed** — pytest-asyncio 1.4.0 (current stable, May 2026) ships `AsyncHypothesisTest` which natively handles `@given` + `async def`. **Verify before converting anything to sync wrappers.** The original issue #258 was resolved in 0.18.0 (January 2022).

2. **Plan bug: `cache_clear()` is wrong** — `_tenant_cache`, `_me_cache`, `_api_key_cache` are `TTLCache` from `cachetools`. The correct method is `.clear()`, **not** `.cache_clear()` (which is the `functools.lru_cache` API). This would raise `AttributeError` at runtime.

3. **gw-m2n security posture must change** — returning `[]` on JSONDecodeError is fail-open: `list_models` treats `allowed = set([])` as "show all models." The fix must raise `TenantConfigError` (or return `None` sentinel), not silently return `[]`.

4. **gw-mbl scope is wider than `docker-compose.yml`** — `proxy/app/config.py:9` also hardcodes `gateway:gateway` as the `DATABASE_URL` field default. Must be changed to `Field(...)` (required) to match the security posture of `jwt_secret`.

5. **gw-b90 module boundary is wrong** — `body.py` is the wrong name/boundary. Correct split: `_jsonb_list` → `proxy/app/db.py`, `_extract_user_message` → `proxy/app/governance_client.py`, `MAX_BODY_SIZE` → class constant on `BodySizeLimitMiddleware`.

6. **`asyncio.get_event_loop()` deprecated** — `auth.py` calls `asyncio.get_event_loop()` (deprecated in Python 3.10, removed in 3.12). Should be `asyncio.get_running_loop()`. Not in original issue list but will surface with async test fixes.

### Key New Findings (Not In Original Issues)

- JWT `roles` claim not validated as list type — `claims.get("roles", [])` with `"roles": "admin"` (string) produces silent misauth
- `DEFAULT_JWT_SECRET` in `gateway_client.py:36` committed to source — not caught by any pre-commit pattern
- `_tenant_cache` can cache stale mocks between Hypothesis examples — must clear at top of each example setup, not just in finally
- Rate limiter mock must be a **new instance per example** (not `reset_mock()`) because slowapi `Limiter` stores internal hit counts
- `config.py` Pydantic v2 `BaseSettings` is mutable by default (not `frozen=True`) — `patch.object` works but bypasses `@model_validator`

---

## Overview

Sixteen bugs across three concern layers:

1. **Infrastructure config** — Docker Compose port defaults and hardcoded credentials (`gw-11d`, `gw-mbl`)
2. **Proxy logic** — `_jsonb_list` error handling and code ordering in `main.py` (`gw-m2n`, `gw-twv`)
3. **Test infrastructure** — `app.state` isolation, Hypothesis/asyncio incompatibility, test correctness, mock deduplication, and JWT shape (`gw-2x9`, `gw-3bi`, `gw-8ph`, `gw-40f`, `gw-p7n`, `gw-b90`, `gw-wsx`, `gw-s20`, `gw-cev`, `gw-ynk`, `gw-3mh`, `gw-1ex`)

---

## Fix Ordering (Mandatory — Dependencies Exist)

SpecFlow analysis + deep research identified these ordering constraints:

```
Step 1 (parallel): gw-11d + gw-mbl (same files, co-land: docker-compose.yml + config.py + Makefile)
Step 2 (parallel): gw-twv, gw-1ex (isolated dead code, zero deps)
Step 3: gw-3bi (VERIFY diagnosis against current headers.py before touching)
Step 4: gw-40f (VERIFY first if pytest-asyncio 1.4.0 already fixes this; if not, establish pattern)
Step 5: gw-8ph (settle settings mock strategy; depends on Step 4 outcome)
Step 6: gw-b90 (move symbols to correct modules, update all import sites)
Step 7: gw-2x9 (async pattern + settings + import paths all settled)
Step 8: gw-m2n (_jsonb_list fix — after import paths stable; audit callers for [] semantics)
Step 9 (parallel): gw-s20, gw-cev, gw-p7n, gw-wsx (independent)
Step 10 (parallel): gw-ynk, gw-3mh (cosmetic)
```

---

## 🔴 P1 — Critical Bugs

### gw-11d: Docker Compose port fallbacks don't match Makefile defaults

**File:** `docker-compose.yml`
**Lines:** Port bindings for proxy (line 76) and postgres (line 9)

**Problem:** Makefile exports `GATEWAY_PROXY_PORT=18765` and `GATEWAY_POSTGRES_PORT=15433`. `docker-compose.yml` fallbacks are `8765` (proxy) and `15432` (postgres). Running `docker compose up` directly (bypassing Make) binds different ports than `make up`, breaking all local integration tests.

```yaml
# docker-compose.yml line 9 — WRONG fallback
- "${GATEWAY_POSTGRES_PORT:-15432}:5432"

# docker-compose.yml line 76 — WRONG fallback
- "${GATEWAY_PROXY_PORT:-8765}:8000"
```

**Fix:**
```yaml
# Match Makefile line 6-7 defaults
- "${GATEWAY_POSTGRES_PORT:-15433}:5432"   # was :-15432
- "${GATEWAY_PROXY_PORT:-18765}:8000"       # was :-8765
```

**⚠️ Must co-land with gw-mbl** (both touch docker-compose.yml and related config files).

### Research Insights — gw-11d

**Deployment concern:** Developers who previously ran `docker compose up` directly have containers bound to 8765/15432. PR description must include:

> If you ran `docker compose up` directly (not via `make up`), stop existing containers before pulling:
> ```bash
> docker compose down   # preserves postgres_data volume
> git pull
> make up
> ```
> Do NOT use `docker compose down -v` — this destroys the postgres data volume.

**CI safety check:** Run before merging:
```bash
grep -r "docker compose up" .github/ .circleci/ 2>/dev/null
```
Any direct `docker compose up` in CI needs `GATEWAY_PROXY_PORT=18765 GATEWAY_POSTGRES_PORT=15433` added to that environment, or changed to `make up`.

**Recommended Makefile addition:**
```makefile
_check-env:
	@if docker volume inspect $$(basename $$PWD)_postgres_data >/dev/null 2>&1; then \
		echo "INFO: postgres_data volume exists. If POSTGRES_PASSWORD changed since init, run: docker compose down && docker volume rm $$(basename $$PWD)_postgres_data && make up"; \
	fi

up: _check-env
	docker compose up -d --wait
```

---

### gw-m2n: `_jsonb_list` uncaught JSONDecodeError and permissive unknown-type fallback

**File:** `proxy/app/main.py:46–59`

**Problem 1 — Uncaught exception:**
```python
def _jsonb_list(value) -> list:
    if isinstance(value, str):
        return json.loads(value)   # ← raises json.JSONDecodeError on malformed JSONB
```

**Problem 2 — Security-critical permissive fallback (the more dangerous bug):**

`list_models` (line 385) uses `if allowed:` — an empty set means "allow all models." Any `_jsonb_list` error path that returns `[]` silently grants unrestricted model access. **This is a security regression masquerading as a bug fix if the patch only adds try/except returning `[]`.**

**asyncpg JSONB behavior (confirmed from docs):** asyncpg returns JSONB columns as Python `str` by default (no auto-decoding). You must register a codec: `await conn.set_type_codec('jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog', format='text')`. Using `format='binary'` for jsonb is broken in asyncpg — always use `format='text'`.

**Fix — three-way semantic distinction:**

```python
class TenantConfigError(Exception):
    """Raised when tenant JSONB config cannot be parsed."""

def _jsonb_list(value: object) -> list[str] | None:
    """Normalize asyncpg JSONB returns to list[str].
    
    Returns:
      None  — value was None (tenant has no allowlist configured → caller decides default)
      []    — explicit empty list from DB (caller should treat as deny-all)
      list  — parsed model list
    Raises:
      TenantConfigError — malformed data; callers must fail-closed
    """
    if value is None:
        return None   # distinct from empty list — "not configured" vs "explicitly empty"
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "_jsonb_list: malformed JSONB for tenant config: %s", exc
            )
            raise TenantConfigError("malformed JSONB") from exc
        if not isinstance(decoded, list):
            raise TenantConfigError(f"JSONB decoded to {type(decoded)}, expected list")
        return [str(x) for x in decoded if isinstance(x, str)]
    if isinstance(value, list):
        return [str(x) for x in value if isinstance(x, str)]
    if isinstance(value, tuple):
        return [str(x) for x in value if isinstance(x, str)]
    raise TenantConfigError(f"unexpected type from asyncpg: {type(value)}")
```

**Callers must be updated to handle `TenantConfigError`:**
- `get_tenant_info` should catch `TenantConfigError` and return HTTP 503, and **must not cache the error result** (the TTLCache would then serve errors for 30 seconds even after the DB is repaired)
- `list_models` and `chat_completions` must treat `None` return as "no restriction configured" (allow-all is intentional) vs `[]` as "explicit empty allowlist" (deny-all)

**Alerting (beyond logging):**
```python
# Add Prometheus/StatsD counter for alerting:
# metric: gateway_tenant_config_parse_errors_total{tenant_id=X, column=Y}
# Spike across tenants = deployment incident; single tenant = data integrity incident
```

### Research Insights — gw-m2n

**asyncpg codec registration pattern** (add to pool init in `proxy/app/main.py` lifespan):
```python
async def init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        'jsonb',
        encoder=json.dumps,
        decoder=json.loads,
        schema='pg_catalog',
        format='text',   # binary format is broken for jsonb — ALWAYS use text
    )

pool = await asyncpg.create_pool(dsn, init=init_connection)
```
If this codec is registered, asyncpg will return pre-parsed Python objects (list/dict/None) instead of strings, eliminating the `json.loads` path entirely. This is a simpler fix than adding try/except to `_jsonb_list` — but requires verifying whether the existing pool uses `init=`.

**Cache invalidation on error:** Ensure `get_tenant_info` does NOT write to `_tenant_cache` when `TenantConfigError` is raised. Current code writes after `_jsonb_list` call — a raised exception naturally avoids the cache write. Verify this is explicit in the fix.

---

### gw-2x9: `app.state` not torn down between Hypothesis examples; cache clears missing from P1–P5 finally blocks

**File:** `proxy/tests/test_properties.py:132–147` and all P1–P5 test bodies

**Problem:** `_setup_app()` sets `app.state.*` fields and `app.dependency_overrides` on the singleton `app`. Hypothesis runs many examples per test. Mock objects accumulate call counts; LRU caches hold stale DB results across examples, corrupting Hypothesis shrinking.

**⚠️ PLAN BUG:** The original plan said `_tenant_cache.cache_clear()` — this is **wrong**. `TTLCache` from `cachetools` uses `.clear()`, not `.cache_clear()`. Using `.cache_clear()` will raise `AttributeError` at runtime. (Only `functools.lru_cache` uses `.cache_clear()`.)

**Fix — extract a `_teardown_app()` helper (simpler than 7 inline lines per test):**

```python
def _teardown_app() -> None:
    """Reset all per-example state. Call in finally of every @given HTTP test."""
    app.dependency_overrides.clear()
    app.state.db_pool = None
    app.state.rate_limiter = None
    app.state.governance_client = None
    # TTLCache.clear(), NOT .cache_clear() — that's lru_cache's API
    _tenant_cache.clear()
    _me_cache.clear()
    _api_key_cache.clear()
    settings.mock_mode = False   # undo gw-8ph's _setup_app mutation (see gw-8ph)
```

**Also call caches at the TOP of each example (before setup), not only in finally:**
```python
def _setup_app(gov_mock=None):
    # Clear BEFORE setting up — previous example's cached data poisons this one
    _tenant_cache.clear()
    _me_cache.clear()
    _api_key_cache.clear()
    ...
```

**Rate limiter — new instance required (not reset_mock):**
The `rate_limiter` mock must be a **fresh instance per example**. Do not use `reset_mock()`. The `Limiter` object stores internal hit counts in its storage backend. A new `_mock_rate_limiter()` call creates a clean mock object; `reset_mock()` resets call tracking but not internal state.

**Scope:** 7 test functions across P1–P5 (P1 has 2, P3 has 3, P4 has 2, P5 has 1). P6 already clears caches but needs the `settings.mock_mode` reset and the `_teardown_app()` consolidation too.

### Research Insights — gw-2x9

**Context manager pattern** (cleaner alternative to try/finally):
```python
from contextlib import contextmanager

@contextmanager
def _app_context(gov_mock=None):
    if gov_mock is None:
        gov_mock = _default_gov_mock()
    pool = _mock_pool()
    _setup_app_state(pool, gov_mock)   # sets app.state.*
    try:
        yield pool, gov_mock
    finally:
        _teardown_app()

# Usage in each @given test:
def test_p1_impl(body):
    asyncio.run(_run(body))

async def _run(body):
    with _app_context() as (pool, gov):
        async with AsyncClient(...) as client:
            response = await client.post(...)
        assert response.status_code in (400, 422)
```

This eliminates all scattered try/finally blocks and guarantees teardown even on assertion failure during Hypothesis shrinking.

---

### gw-3bi: U1 test passes `required_roles` to wrong positional slot of `error_envelope`

**File:** `proxy/tests/test_properties.py` (U1 test block)

**⚠️ Diagnosis must be verified before implementation.** SpecFlow analysis found a discrepancy between the original issue description and current code.

The current `error_envelope` signature in `proxy/app/headers.py:36` is:
```python
def error_envelope(
    error_type: str,
    message: str,
    violations: Sequence[str] = (),
    required_roles: Sequence[str] = (),
    approved_providers_for_classification: Sequence[str] = (),
) -> dict:
```

If U1 calls `error_envelope(error_type, message, violations, required_roles)` positionally, this matches the signature exactly. The more likely actual bug is:
- The test asserts `body["violations"] == list(violations)` but **never asserts** `body.get("required_roles")` — meaning `required_roles` is passed but the assertion never verifies the output was correct.

**Action before coding:** Read the U1 test block in `test_properties.py` and `headers.py:36–52` and determine: is the argument order wrong, or are the assertions incomplete? **If assertions are incomplete, add assertions — do NOT reorder arguments (that would introduce the bug the issue claims to fix).**

---

### gw-twv: `_MALFORMED_BYTES` dead code + forward reference to `_is_valid_json_object`

**File:** `proxy/tests/test_properties.py:169–179`

**Problem 1 — Forward reference:** `_MALFORMED_BYTES` at line 169 references `_is_valid_json_object` defined at line 174. Python lambda bodies are late-binding (evaluated at call time, not definition time), so this doesn't raise `NameError` at import — but it is fragile and will break on any rename/move.

**Problem 2 — Dead code:** `_MALFORMED_BYTES` is never used. `test_p1_binary_garbage_never_500` uses `st.binary()` directly with an inline `if _is_valid_json_object(raw): return` guard.

**Fix (recommended: use the strategy, remove the inline guard):**
```python
# Move _is_valid_json_object BEFORE _MALFORMED_BYTES
def _is_valid_json_object(data: bytes) -> bool:
    try:
        return isinstance(json.loads(data), dict)
    except (json.JSONDecodeError, ValueError):
        return False

# Strategy defined AFTER the helper it references
_MALFORMED_BYTES = st.binary(min_size=1, max_size=512).filter(
    lambda b: not _is_valid_json_object(b)
)

# Use _MALFORMED_BYTES in the test — eliminates inline duplication
@given(raw=_MALFORMED_BYTES)   # was: st.binary(...).filter(...)
@_FAST
def test_p1_binary_garbage_never_500(raw):
    asyncio.run(_impl_p1_binary(raw))
    # No inline 'if _is_valid_json_object(raw): return' needed
```

**Why `.filter()` over inline `if ... return`:** The inline `return` counts as a "passed" test from Hypothesis's view (no assertion failure), skewing example distribution and silently reducing effective coverage. `.filter()` rejects at draw time and Hypothesis never counts filtered draws as examples.

---

### gw-8ph: `_setup_app` silently requires `settings.mock_mode=True`; fails if `MOCK_PROVIDERS` not set

**File:** `proxy/tests/test_properties.py:132–147`

**Problem:** `_setup_app` directly assigns `settings.mock_mode = True` on the module-level Pydantic `BaseSettings` singleton. Two issues:
1. No isolation — mutation persists across tests
2. Pydantic v2 `@model_validator` computes `mock_mode` from `mock_providers` — direct assignment bypasses this validator

**Simplest correct fix:** Add `settings.mock_mode = False` to `_teardown_app()` (see gw-2x9). Since `_teardown_app()` is called in every `finally` block, this is automatically scoped to each example.

**Stronger fix (eliminates singleton mutation entirely):**
```python
# In proxy/tests/helpers.py
def create_test_settings(**overrides) -> Settings:
    """Create a fresh Settings instance with test defaults. Use instead of mutating the singleton."""
    return Settings.model_validate(
        {
            "mock_mode": True,
            "mock_providers": True,
            "jwt_secret": "test-jwt-secret-for-tests-only-32chars!!",
            **overrides,
        },
        context={"_env_file": None},  # don't read .env during test
    )
```

**Pydantic v2 `patch.object` caveat:** `patch.object(settings, "mock_mode", True)` bypasses the `@model_validator`, so you can set `mock_mode = True` while `mock_providers = False` — a state the validator would normally prevent. This is fine for tests (you want to force the state), but the plan must document this explicitly so implementors don't accidentally patch `mock_providers` and wonder why `mock_mode` doesn't follow.

### Research Insights — gw-8ph

**Which `_setup_app` variant sets `mock_mode`:** Confirm whether P6 sets `mock_mode` (it doesn't call `_setup_app` — it manually sets `app.state`). If P6 tests behavior under `mock_mode = False` (real providers), adding `settings.mock_mode = False` to `_teardown_app` would cause P6 to work correctly by default.

---

## 🟡 P2 — Important Bugs

### gw-mbl: `governance` and `proxy` services hardcode `gateway:gateway` in DATABASE_URL

**Files:** `docker-compose.yml:55,80` and `proxy/app/config.py:9`

**Problem — docker-compose.yml:**
```yaml
# governance service (line 55) — hardcoded:
DATABASE_URL: postgresql+asyncpg://gateway:gateway@postgres:5432/gateway

# proxy service (line 80) — hardcoded:
DATABASE_URL: postgresql+asyncpg://gateway:gateway@postgres:5432/gateway

# migrate service (line 44) — CORRECT:
DATABASE_URL: postgresql+asyncpg://gateway:${POSTGRES_PASSWORD:-gateway}@postgres:5432/gateway
```

**Problem — config.py (new finding from research):**
```python
# proxy/app/config.py line 9 — also hardcoded:
database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
```
This means if `DATABASE_URL` is unset in environment, the proxy starts silently with the default credential. Should be `Field(...)` (required) to match `jwt_secret` and `governance_internal_token`.

**Fix — docker-compose.yml lines 55 and 80:**
```yaml
DATABASE_URL: postgresql+asyncpg://gateway:${POSTGRES_PASSWORD:-gateway}@postgres:5432/gateway
```

**Fix — Makefile line 9:**
```makefile
DATABASE_URL ?= postgresql://gateway:${POSTGRES_PASSWORD:-gateway}@localhost:$(GATEWAY_POSTGRES_PORT)/gateway
```

**Fix — proxy/app/config.py:**
```python
database_url: str = Field(..., description="PostgreSQL connection string (required)")
```

**⚠️ Failure mode for password mismatch:** Auth failure (FATAL: password authentication failed for user "gateway"), NOT connection refused. Postgres validates the password against its own internal state set at `initdb` time. If `POSTGRES_PASSWORD` is changed and the container is not re-initialized, the entire proxy stack fails with 503s on every request. Mitigation: the `_check-env` Makefile target (see gw-11d) warns about volume state.

**⚠️ co-land with gw-11d** — both touch docker-compose.yml.

---

### gw-40f: `@pytest.mark.asyncio` + `@given` on async functions

**File:** `proxy/tests/test_properties.py` — all `test_p1_` through `test_p6_` tests

**⚠️ VERIFY BEFORE IMPLEMENTING:** Research confirms that pytest-asyncio **0.18.0** (January 2022) fixed the core incompatibility described in issue #258, via `AsyncHypothesisTest`. The project uses pytest-asyncio **1.4.0** (current stable, May 2026). The existing `@pytest.mark.asyncio` + `@given` + `async def` pattern is the documented supported pattern in 1.4.0.

**Verification step (run first):**
```bash
cd proxy && uv run pytest tests/test_properties.py -x -v --tb=short 2>&1 | head -80
```

**If tests pass** — gw-40f may not require test restructuring. The issue was already fixed upstream. The `_setup_app()` isolation approach is already the correct workaround for the "async fixtures can't be used with @given" limitation (which remains).

**If "Event loop is closed" or "coroutine was never awaited" errors persist** — use the sync wrapper pattern with `asyncio.run()` **only**:

```python
import asyncio

@given(body=_NON_OBJECT_JSON)
@_FAST
def test_p1_non_object_json_never_500(body):    # sync def — Hypothesis drives this
    asyncio.run(_p1_impl(body))

async def _p1_impl(body):
    with _app_context() as (pool, gov):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                content=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
    assert response.status_code in (400, 422)
```

**Do NOT use:**
- `anyio.from_thread.run_sync` — this runs sync code from inside async contexts; wrong direction entirely
- `asyncio_default_fixture_loop_scope = "session"` — controls fixture loop scope, does not fix hypothesis/async incompatibility, and breaks per-example isolation

**Also fix:** Add `deadline=None` to the `_FAST` settings profile regardless of which approach is taken:
```python
_FAST = settings(
    max_examples=50,
    deadline=None,   # ASGI startup regularly exceeds 200ms default; avoid flaky CI failures
    suppress_health_check=[HealthCheck.too_slow],
)
```

**Scope:** P1 (2 tests), P2, P3 (3 tests), P4 (2 tests), P5, P6 — **7 test functions total**. Remove `@pytest.mark.asyncio` from all of them if converting to sync wrappers (it only applies to `async def` tests).

### Research Insights — gw-40f

**`asyncio.get_event_loop()` deprecation (bonus fix):** While fixing async patterns, also update `auth.py` which calls `asyncio.get_event_loop()` (deprecated in Python 3.10+, raises `DeprecationWarning` in 3.10-3.11, removed in 3.12). Replace with `asyncio.get_running_loop()`.

---

### gw-p7n: Manual JWT in `gateway_client.py` missing `iat`/`nbf` claims

**File:** `apps/local-integration-tests/gateway_client.py:46–69`

**python-jose claim validation behavior (confirmed from source):**
- `verify_iat = True` — only validates it's an integer. **Not a time check.** Does not reject future-issued tokens.
- `verify_nbf = True` — real time check when present. Rejects tokens where `nbf > now`.
- `verify_jti = True` — only validates it's a string. **No replay prevention built in.**
- All `verify_*` flags **silently skip absent claims**. Must pair with `require_*: True` for presence enforcement.

**Recommended JWT payload (both `gateway_client.py` and `conftest.make_jwt`):**
```python
now = int(time.time())
payload = {
    "sub": user_id,
    "tenant_id": tenant_id,
    "roles": roles,
    "exp": now + exp_seconds,
    "iat": now,                        # add: required for audit trail
    "nbf": now,                        # add: activates nbf time check
    "iss": "llm-gateway",              # add: issuer validation
    "jti": secrets.token_hex(16),      # add: unique token ID (replay prevention foundation)
}
```

**Server-side jwt.decode options update:**
```python
options = {
    "require": ["exp", "iat", "nbf"],   # presence enforcement
    "leeway": 30,                        # 30-second clock skew tolerance
}
jwt.decode(token, secret, algorithms=["HS256"], options=options)
```

**Replay prevention with `jti` (if implementing):** Store `jti` in Redis with TTL = remaining token TTL (`exp - now`). Reject tokens whose `jti` is already in the store. The gateway has Redis available via `app.state.redis`.

**Additional security fix — roles validation (new finding):**
```python
# auth.py — current:
roles = claims.get("roles", [])   # if "roles": "admin" (string), this is a string

# Fix: validate roles is a list
roles = claims.get("roles", [])
if not isinstance(roles, list):
    raise JWTError("roles claim must be a list")
roles = [str(r) for r in roles if isinstance(r, str)]
```

**Both locations must update together** — `gateway_client.py` and `proxy/tests/conftest.py:make_jwt` — to keep JWT shape consistent between integration and unit tests.

---

### gw-b90: Private symbols imported from `main.py` — extract to correct modules

**Files:** `proxy/app/main.py`, tests that import `_jsonb_list`, `_extract_user_message`, `MAX_BODY_SIZE`

**Problem:** Tests import module-private symbols from `main.py`. The plan originally proposed `proxy/app/body.py` — this is the wrong module boundary.

**Correct architecture (from architecture review):**

| Symbol | Move to | Rationale |
|--------|---------|-----------|
| `_jsonb_list` | `proxy/app/db.py` | DB serialization concern; lives next to `_asyncpg_dsn` and pool utilities |
| `_extract_user_message` | `proxy/app/governance_client.py` | Exists solely to prepare `InspectRequest.text`; tightly coupled to governance path |
| `MAX_BODY_SIZE` | Class constant on `BodySizeLimitMiddleware` in `proxy/app/middleware.py` | Belongs with the middleware that uses it |

**New files required:**
- `proxy/app/db.py` — new module for DB utilities (`_jsonb_list`, `_asyncpg_dsn` from main.py)
- `proxy/app/middleware.py` — extract `BodySizeLimitMiddleware` + `MAX_BODY_SIZE` from main.py

**Rename at extraction time:** Drop leading underscores from these three symbols — they are conventionally "private" only because they live in `main.py`. As public API of their new home modules they should be `jsonb_list`, `extract_user_message` (in governance_client), and `MAX_BODY_SIZE` (as a class constant, already no underscore).

**Before moving — find all import sites:**
```bash
rg "from proxy.app.main import" proxy/tests/
rg "_jsonb_list\|_extract_user_message\|MAX_BODY_SIZE" proxy/
```

---

### gw-wsx: `_load_root_env()` called at module import time

**File:** `apps/local-integration-tests/gateway_client.py:16–32`

**Fix:** Move `_load_root_env()` call from module level to `GatewayClient.__init__`:
```python
class GatewayClient:
    def __init__(self, base_url: str | None = None, ...):
        _load_root_env()   # now scoped to instance creation, not module import
        self.base_url = base_url or os.environ.get(
            "GATEWAY_BASE_URL",
            f"http://localhost:{os.environ.get('GATEWAY_PROXY_PORT', '18765')}"
        )
```

**Additional fix required (root conftest.py):** The root-level `proxy/conftest.py` is currently empty of content. Required env vars (`GOVERNANCE_INTERNAL_TOKEN`, `JWT_SECRET`) must be set before any `proxy.app.*` module import:
```python
# proxy/conftest.py
import os
os.environ.setdefault("GOVERNANCE_INTERNAL_TOKEN", "test-governance-token")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-tests-only-32chars!!")
os.environ.setdefault("MOCK_PROVIDERS", "true")
```
`setdefault` ensures shell env vars take precedence over test defaults.

---

### gw-s20: Mock helpers duplicated between `test_properties.py` and `conftest.py`

**Files:** `proxy/tests/test_properties.py:97–129`, `proxy/tests/conftest.py:65–115`

**Problem:** Three helpers duplicated with subtle signature divergences:
- `_mock_pool`: conftest takes `fetchrow_result=None`; test_properties takes no params
- `_mock_rate_limiter`: conftest takes `allowed: bool = True`; test_properties has no param (always allowed=True)
- `_MODELS_CONFIG`: constant duplicated verbatim in both files

**Fix — create `proxy/tests/helpers.py`:**
```python
# proxy/tests/helpers.py — plain functions, importable by both conftest (as fixtures)
# and test_properties (directly, since @given can't use pytest fixtures)

def make_mock_pool(fetchrow_result=None) -> AsyncMock:
    """Use conftest signature (superset)."""
    ...

def make_mock_rate_limiter(allowed: bool = True) -> RateLimitResult:
    """Use conftest signature (superset). Always creates a fresh instance."""
    ...

def make_default_gov_mock(audit_id: str = "test-audit-id") -> AsyncMock:
    """Fresh instance per call — never reuse across examples."""
    ...

# Single canonical models config
MODELS_CONFIG = {...}   # move from both files here
```

**conftest.py:** Remove local definitions, import from helpers:
```python
from .helpers import make_mock_pool, make_mock_rate_limiter, make_default_gov_mock, MODELS_CONFIG

@pytest.fixture
def mock_pool(fetchrow_result=None):
    return make_mock_pool(fetchrow_result)
```

**test_properties.py:** Import directly from helpers:
```python
from .helpers import make_mock_pool, make_mock_rate_limiter, make_default_gov_mock, MODELS_CONFIG
```

**Do NOT add `request.param` wrapper pattern to conftest fixtures** — no current test uses parametrize with these fixtures (YAGNI).

**Signature reconciliation required:**
- The unified `_mock_rate_limiter` must use the conftest signature (accepts `allowed: bool`) and update all `test_properties.py` call sites that currently call it with no args (which means `allowed=True` — the default, no change needed)
- The unified `_default_gov_mock` should accept `audit_id` parameter with `"test-audit-id"` as default, and test_properties callers that used `"pbt-audit-id"` should be updated to the unified default (the audit_id strings have no behavioral significance)

---

## 🔵 P3 — Nice To Have

### gw-ynk: `__import__('re')` inside Hypothesis filter lambdas

**File:** `proxy/tests/test_properties.py` (strategy definitions)

**Fix — highest-impact test performance improvement in the plan:**
```python
import re                          # module-level import

_SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")   # compile once
_BLOCKED_KEYWORDS = frozenset(
    ("diagnosis", "patient", "ignore previous", "disregard system", "gpt-4o")
)

def _is_clean_content(s: str) -> bool:
    """Named function for clarity and deduplication between _CLEAN_CONTENT + _UNICODE_CONTENT."""
    s_lower = s.lower()
    return (
        not any(kw in s_lower for kw in _BLOCKED_KEYWORDS)
        and not _SSN_RE.search(s)
    )

_CLEAN_CONTENT = st.text(...).filter(_is_clean_content)     # pass function ref
_UNICODE_CONTENT = st.text(...).filter(_is_clean_content)   # reuse same function
```

**Performance note:** `_CLEAN_CONTENT` and `_UNICODE_CONTENT` strategies are evaluated hundreds of times per test run. Moving from `__import__('re').search()` to `_SSN_RE.search()` (compiled once) and a named function (no lambda overhead) reduces filter lambda cost by ~20–40%. This is the highest-impact performance fix in the test suite.

Also: the filter logic in `_CLEAN_CONTENT` and `_UNICODE_CONTENT` appears to be identical — deduplication to a single `_is_clean_content` function eliminates a hidden divergence risk.

---

### gw-3mh: `GatewayClient.base_url` default evaluated at class-body time

**File:** `apps/local-integration-tests/gateway_client.py`

**Fix:**
```python
def __init__(self, base_url: str | None = None) -> None:
    self.base_url = base_url or os.environ.get(
        "GATEWAY_BASE_URL",
        f"http://localhost:{os.environ.get('GATEWAY_PROXY_PORT', '18765')}"
    )
```

**Type annotation concern:** If any call sites use `GatewayClient.DEFAULT_BASE_URL` as a class attribute, those must be updated too. Check with `rg "DEFAULT_BASE_URL" apps/`.

---

### gw-cev: Single local-integration-user with `admin+tier1` — split into two users

**Files:** `config/users.yaml:29–34`, `scripts/provision.py`, `apps/local-integration-tests/gateway_client.py`

**Fix — add second user to users.yaml:**
```yaml
- id: local-integration-user-tier1-only
  tenant_id: local-integration
  roles:
    - tier1
  initial_key: "REPLACE_IN_PROVISIONER"
```

**gateway_client.py — change DEFAULT_ROLES to least privilege:**
```python
DEFAULT_ROLES = ["tier1"]    # was ["admin", "tier1"] — principle of least privilege for default
```
Tests that genuinely need admin should pass `roles=["admin", "tier1"]` explicitly.

**Required integration test assertions:**
- Tier1-only user gets HTTP 403 on admin-protected endpoints (e.g., `GET /admin/audit`)
- Admin-only user (without tier1) is denied tier1 model access
- These are the specific invariants that justify the PoLP test split

**provision.py update:** Add provisioning for `local-integration-user-tier1-only` using the same bcrypt API key generation pattern as the existing user.

---

### gw-1ex: `BEARER_PATTERN` defined but never used in `pre-commit-security.sh`

**File:** `scripts/pre-commit-security.sh:23`

**Fix — add scan to `check_file()`:**
```bash
# Add alongside SECRET_KEY_PATTERN, AWS_KEY_PATTERN, GENERIC_SECRET_PATTERN checks:
if echo "$added_lines" | grep -qiP "$BEARER_PATTERN"; then
    add_finding "$file" "$line_num" "Bearer token" "$BEARER_PATTERN"
    FAIL=1
fi
```

The 20-character minimum in `BEARER_PATTERN='Bearer [a-zA-Z0-9_\-\.]{20,}'` prevents false positives on short test values like `"Bearer testtoken"`. Confirm this threshold is intentional.

**Additional security gap found:** `DEFAULT_JWT_SECRET = "local-dev-jwt-secret-for-compose-tests-only"` in `gateway_client.py:36` is committed to source. `GENERIC_SECRET_PATTERN` (requires assignment context like `secret=`) would catch `jwt_secret = "..."` in configs but NOT this Python constant. Consider adding a pattern for committed dev secrets, or an explicit allowlist comment that the pre-commit hook recognizes.

---

## Acceptance Criteria

### Infrastructure
- [ ] `docker compose up` (without Make) binds proxy to 18765, postgres to 15433 — identical to `make up`
- [ ] `POSTGRES_PASSWORD=mysecret docker compose up` propagates `mysecret` to governance, proxy, and migrate service DATABASE_URLs
- [ ] `POSTGRES_PASSWORD=mysecret make up` propagates `mysecret` to Makefile `DATABASE_URL`
- [ ] `proxy/app/config.py` `database_url` field has no default value (required)
- [ ] `make up` prints a warning if `postgres_data` volume exists and `POSTGRES_PASSWORD` may have changed

### Proxy Logic
- [ ] `_jsonb_list` with a malformed JSON string raises `TenantConfigError` (not 500)
- [ ] `_jsonb_list` with an unexpected type raises `TenantConfigError` (not silent `[]`)
- [ ] `_jsonb_list(None)` returns `None` (caller treats as "no restriction configured")
- [ ] `_jsonb_list("[]")` returns `[]` (caller treats as "explicit empty allowlist = deny-all")
- [ ] `get_tenant_info` catches `TenantConfigError` and returns 503 — does NOT write error result to `_tenant_cache`
- [ ] `list_models` and `chat_completions` treat `None` allowed_models as allow-all, `[]` as deny-all
- [ ] Property test `test_u3_jsonb_list_invariants` passes when `st.text()` generates invalid JSON
- [ ] `_is_valid_json_object` is defined before `_MALFORMED_BYTES` in `test_properties.py`
- [ ] `_MALFORMED_BYTES` is used by `test_p1_binary_garbage_never_500` (not dead code)

### Test Infrastructure
- [ ] All P1–P6 property tests pass with Hypothesis shrinking enabled (no "Event loop closed" errors)
- [ ] Running any P1–P6 test multiple times in sequence produces identical results (no state bleed)
- [ ] `app.state.db_pool`, `app.state.rate_limiter`, `app.state.governance_client` are `None` after each Hypothesis example
- [ ] `_tenant_cache.clear()`, `_me_cache.clear()`, `_api_key_cache.clear()` called BEFORE AND AFTER each example
- [ ] `settings.mock_mode` is reset to `False` after each example
- [ ] `_teardown_app()` helper exists and is called from every P1–P6 finally block
- [ ] U1 test assertions include `body.get("required_roles")` verification — not just `violations`
- [ ] `proxy/tests/helpers.py` contains `make_mock_pool`, `make_mock_rate_limiter`, `make_default_gov_mock`, `MODELS_CONFIG`
- [ ] No duplicate mock helpers in `conftest.py` or `test_properties.py`
- [ ] `_setup_app` does not leave `settings.mock_mode = True` visible after each example
- [ ] `_fast` settings profile includes `deadline=None`

### Module Organization
- [ ] `proxy/app/db.py` exports `jsonb_list` (formerly `_jsonb_list`) and DB utilities
- [ ] `_extract_user_message` moved to `proxy/app/governance_client.py`
- [ ] `MAX_BODY_SIZE` is a class constant on `BodySizeLimitMiddleware`
- [ ] No test imports private symbols directly from `proxy.app.main`

### JWT & Auth
- [ ] `gateway_client.make_jwt()` includes `iat`, `nbf`, `iss`, `jti` claims
- [ ] `conftest.make_jwt()` includes `iat`, `nbf`, `iss` claims (minimum)
- [ ] `auth.py` validates `roles` claim is a list type before use
- [ ] `_load_root_env()` is not called at module import time in `gateway_client.py`
- [ ] `proxy/conftest.py` sets required env var defaults before any `proxy.app` import

### Security
- [ ] `pre-commit-security.sh` `check_file()` scans for `BEARER_PATTERN`
- [ ] Two distinct integration users: `local-integration-user` (admin+tier1) and `local-integration-user-tier1-only` (tier1)
- [ ] Integration test asserts tier1-only user gets 403 on admin endpoint
- [ ] `gateway_client.DEFAULT_ROLES` is `["tier1"]` (least privilege)
- [ ] `auth.py` uses `asyncio.get_running_loop()` not deprecated `asyncio.get_event_loop()`

---

## System-Wide Impact

### Interaction Graph
- `_jsonb_list` → `get_tenant_info` → `_tenant_cache` → every `chat_completions` and `list_models` request. The `TenantConfigError` path must ensure the cache is NOT written.
- `gw-b90` symbol extraction → changes import graph for `main.py`, `test_properties.py`, `tests/integration/test_smoke.py`. Run `rg "from proxy.app.main import"` to find all sites.
- `gw-40f` verification → if tests already work with pytest-asyncio 1.4.0, no test restructuring needed. This changes the estimated effort for the entire P1 test infrastructure block.

### Error Propagation
- `TenantConfigError` from `_jsonb_list` → caught in `get_tenant_info` → HTTP 503. This must not cache. Clients will retry.
- `settings.mock_mode` mutation in `_setup_app` → bleeds across tests if not reset. The `_teardown_app()` helper's `settings.mock_mode = False` ensures cleanup.

### State Lifecycle Risks
- `_tenant_cache` (TTLCache): `.clear()` not `.cache_clear()` — this is a **plan bug** that must be fixed
- `app.state` is a module-level singleton — must be reset per Hypothesis example, not per test function
- `settings` is a Pydantic v2 singleton — mutable by default; mutations must be paired with explicit teardown

### API Surface Parity
- `conftest.make_jwt` and `gateway_client.make_jwt` are parallel JWT factories. After gw-p7n, both must produce tokens with the same claims (`sub`, `tenant_id`, `roles`, `exp`, `iat`, `nbf`, `iss`).
- `jsonb_list` in `db.py` and the inline JSONB handling (if asyncpg codec is registered) — verify both paths are covered.

### Integration Test Scenarios (Cross-Layer)
1. Tenant with malformed `allowed_models` JSONB in DB → proxy returns 503 (not 500, not allow-all)
2. `POSTGRES_PASSWORD` override → all three services (migrate, governance, proxy) connect successfully
3. Bearer token in staged git commit → pre-commit hook blocks the commit
4. Tier1-only user requests admin endpoint → proxy returns 403
5. JWT without `iat` or `nbf` → proxy rejects with 401 if `require_iat` and `require_nbf` are enabled server-side

---

## Dependencies & Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| gw-3bi diagnosis is wrong — applying described fix introduces regression | Medium | High | Read `headers.py:36–52` and U1 call site before touching anything |
| gw-40f may already be fixed in pytest-asyncio 1.4.0 | High | Medium | Run `pytest test_properties.py -x` and check for event loop errors |
| gw-b90 symbol move breaks smoke tests importing from `proxy.app.main` | Medium | Low | `rg "from proxy.app.main import"` finds all sites before moving |
| `_tenant_cache.cache_clear()` in plan text causes AttributeError | High (plan bug) | High | Fix to `.clear()` in all implementations |
| Cache poisoning: error sentinel cached in `_tenant_cache` | Medium | High | Ensure `TenantConfigError` path does not write to cache |
| `asyncio.get_event_loop()` deprecated in auth.py | Low | Low | Fix alongside gw-40f verification |

---

## Files to Modify

| File | Changes |
|------|---------|
| `docker-compose.yml` | Port fallbacks (gw-11d), password parameterization (gw-mbl) |
| `Makefile` | DATABASE_URL password parameterization (gw-mbl), `_check-env` target (gw-11d) |
| `proxy/app/main.py` | Remove symbols after gw-b90 extraction; `_jsonb_list` exception handling (gw-m2n) |
| `proxy/app/db.py` | **NEW** — `jsonb_list`, `_asyncpg_dsn` (gw-b90) |
| `proxy/app/middleware.py` | **NEW** — `BodySizeLimitMiddleware`, `MAX_BODY_SIZE` (gw-b90) |
| `proxy/app/governance_client.py` | Add `extract_user_message` (moved from main.py, gw-b90) |
| `proxy/app/config.py` | `database_url = Field(...)` required (gw-mbl extended) |
| `proxy/app/headers.py` | Verify `error_envelope` signature (gw-3bi) |
| `proxy/app/auth.py` | `asyncio.get_running_loop()` (new finding); roles list validation (gw-p7n extended) |
| `proxy/tests/test_properties.py` | Dead code removal (gw-twv), app.state teardown (gw-2x9), async fix (gw-40f), U1 assertion (gw-3bi), `_setup_app` isolation (gw-8ph), mock deduplication (gw-s20), `re.compile` (gw-ynk) |
| `proxy/tests/conftest.py` | Mock deduplication (gw-s20), `make_jwt` claims (gw-p7n), root env defaults |
| `proxy/tests/helpers.py` | **NEW** — shared mock helpers (gw-s20) |
| `proxy/conftest.py` | Add env var defaults before proxy.app imports (gw-wsx extended) |
| `apps/local-integration-tests/gateway_client.py` | `_load_root_env` import-time call (gw-wsx), `base_url` default (gw-3mh), JWT claims (gw-p7n), `DEFAULT_ROLES` (gw-cev) |
| `config/users.yaml` | Second integration user (gw-cev) |
| `scripts/provision.py` | Provision new integration user (gw-cev) |
| `scripts/pre-commit-security.sh` | Bearer pattern scan (gw-1ex) |

---

## Sources & References

### Internal References
- `proxy/app/main.py:46–59` — `_jsonb_list` current implementation
- `proxy/app/headers.py:36–52` — `error_envelope` canonical signature
- `proxy/app/auth.py:74–82` — `_validate_jwt`, `asyncio.get_event_loop()` calls
- `proxy/app/config.py:9` — hardcoded DATABASE_URL default
- `proxy/tests/test_properties.py:97–147,169–179` — duplicate mocks, dead code, `_setup_app`
- `proxy/tests/conftest.py:54–157` — fixtures and cache clearing
- `apps/local-integration-tests/gateway_client.py:16–69` — `_load_root_env`, `make_jwt`, `DEFAULT_BASE_URL`, `DEFAULT_ROLES`
- `docker-compose.yml:9,44,55,76,80` — port and credential issues
- `Makefile:6–9` — authoritative port defaults
- `config/users.yaml:29–34` — single integration user
- `scripts/pre-commit-security.sh:19–25` — patterns (including unused `BEARER_PATTERN`)

### External References
- pytest-asyncio issue #258 + PR #259: `@given` + `async def` — fixed in 0.18.0 (Jan 2022)
- pytest-asyncio `AsyncHypothesisTest` — handles `inner_test` wrapping
- asyncpg JSONB codec: always use `format='text'`, not `format='binary'`
- python-jose: `verify_iat` is integer-only check (not time check); `verify_jti` is string-only (no replay store)
- Hypothesis `HealthCheck.function_scoped_fixture`: suppress only for read-only fixtures
- cachetools `TTLCache`: `.clear()` not `.cache_clear()` (the latter is `functools.lru_cache`)
- Pydantic v2 `BaseSettings`: mutable by default; `patch.object` bypasses `@model_validator`
