# Integration Tests Design

**Date:** 2026-05-27  
**Status:** Approved

## Goal

Build a two-tier test suite for the AI Gateway proxy: fast in-process tests using ASGI transport (no Docker required), plus a Docker Compose smoke test that validates the full stack end-to-end.

## Scope

- Proxy service only (`proxy/tests/`)
- All six governance scenarios (clean, PII redact, PHI block, prompt injection, model-tier deny, rate-limit)
- Auth paths: JWT and API key (both success and failure)
- Admin RBAC on key management, audit, and user deletion routes
- Response shape and header assertions
- Docker smoke test for /health, /v1/models, /v1/chat/completions, and unauthenticated 401

Not in scope: governance service tests (already in `governance/tests/`), OPA policy tests (`make opa-test`), load/performance testing.

## File Structure

```
proxy/
├── conftest.py                     # env var defaults + sys.path for project root
└── tests/
    ├── conftest.py                 # fixtures: async_client, gov_mock, clear_caches, make_jwt
    ├── test_health.py
    ├── test_chat.py
    ├── test_auth.py
    ├── test_admin.py
    └── test_models.py

tests/
└── integration/
    ├── __init__.py
    └── test_smoke.py               # requires INTEGRATION_TEST=1
```

The Makefile `test` target already runs `cd proxy && uv run pytest tests/`. No changes to pytest config are needed.

## In-Process Fixture Architecture

### Path and environment setup (`proxy/conftest.py`)

Sets required env vars at module level **before** proxy modules are imported (Pydantic Settings reads them at instantiation time):

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-tests-only")
os.environ.setdefault("GOVERNANCE_INTERNAL_TOKEN", "test-gov-token")
os.environ.setdefault("MOCK_PROVIDERS", "true")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
```

### Core fixtures (`proxy/tests/conftest.py`)

**`clear_caches` (autouse, function scope)**  
Clears `_tenant_cache`, `_me_cache`, and `_api_key_cache` before and after every test. These TTLCache singletons persist across tests and would cause false passes.

**`gov_mock`**  
An `AsyncMock` of `GovernanceClient`. Defaults to `decision="allow"`, empty PII findings, empty violations. Individual tests reconfigure `.inspect.return_value` to exercise block/PII paths.

**`async_client(gov_mock)`**  
Primary fixture. Sets `app.dependency_overrides[get_caller]` to return a fixed `CallerContext(user_id="test-user", tenant_id="test-tenant", roles=["user"])`. Then patches four symbols before entering the ASGI lifespan context:

| Symbol | Mock |
|---|---|
| `asyncpg.create_pool` | `AsyncMock` → mock pool; `conn.fetchrow` returns `None` (tenant uses defaults) |
| `redis.asyncio.Redis.from_url` | Mock Redis; `script_load` → sha string; `evalsha` → `[1, 0]` (allowed) |
| `proxy.app.bootstrap.maybe_bootstrap` | `AsyncMock()` no-op |
| `proxy.app.main.make_governance_client` | Returns `gov_mock` |

Yields `(client, gov_mock)`. Clears `dependency_overrides` in teardown.

**`admin_client(gov_mock)`**  
Same as `async_client` but caller has `roles=["admin"]`.

**`make_jwt`**  
Factory function (not a fixture): `make_jwt(user_id, tenant_id, roles)` → signed HS256 JWT using the test secret. Used in auth tests where `get_caller` is NOT overridden.

### Rate limit control

After the lifespan runs, `app.state.rate_limiter` is a live `RateLimiter` backed by mock Redis. Tests that need a denial configure `app.state.rate_limiter.check` with an `AsyncMock` returning `RateLimitResult(allowed=False, retry_after_seconds=60, ...)`.

### Governance control

Tests that exercise governance blocking reconfigure `gov_mock.inspect.return_value`:
```python
gov_mock.inspect.return_value = InspectResponse(
    decision="block", violations=["policy:data_classification_mismatch"], ...
)
```

## Tests

### test_health.py

| Test | Assertion |
|---|---|
| `test_health_ok` | GET /health → 200, body `{"status":"ok"}` |
| `test_health_not_ready` | Set `app.state.ready = False`, GET /health → 503 |

### test_chat.py

| Test | Setup | Expected |
|---|---|---|
| `test_clean_request` | Default gov_mock (allow) | 200; body has `choices[0].message.content`, `usage.total_tokens` |
| `test_rate_limit_denied` | Override `app.state.rate_limiter.check` → denied | 429; headers `Retry-After`, `retry-after-ms`, `x-ratelimit-limit-requests` |
| `test_governance_blocks` | `gov_mock` returns block + violations | 403; body `error.type == "policy_violation"`, `error.violations` non-empty |
| `test_pii_redaction` | `gov_mock` returns allow + `pii_findings=[{"type":"SSN"}]` + `redacted_text` | 200; header `X-Gateway-Pii-Redacted: true`; `X-Gateway-Pii-Types: SSN` |
| `test_phi_block_via_mock_provider` | gov_mock allows; message contains "diagnosis" | 403 (mock provider blocks) |
| `test_unknown_model` | message with model not in yaml, no prefix match | 400; `error.type == "model_not_found"` |
| `test_streaming` | `stream: true` in body | 200; content-type `text/event-stream`; response body contains `data:` SSE lines |
| `test_governance_unavailable` | `gov_mock.inspect` raises `GovernanceError` | 503; `error.type == "governance_unavailable"` |

### test_auth.py

These tests do NOT use `get_caller` override. They test real auth code paths with mock DB.

| Test | Setup | Expected |
|---|---|---|
| `test_jwt_valid` | Bearer token from `make_jwt()` | 200 |
| `test_jwt_tampered` | Bearer token signed with wrong key | 401 |
| `test_no_auth` | No Authorization header | 401 |
| `test_api_key_valid` | ApiKey prefix; mock pool returns bcrypt-hashed row | 200 |
| `test_api_key_invalid` | ApiKey prefix; mock pool returns `None` | 401 |

For API key tests: the mock pool's `conn.fetchrow` returns a row dict with a pre-computed bcrypt hash of the test key. The key prefix identifies the row lookup.

### test_admin.py

| Test | Caller | Endpoint | Expected |
|---|---|---|---|
| `test_create_key_admin` | admin | POST /v1/keys | 200; body has `key` string |
| `test_create_key_non_admin` | user | POST /v1/keys | 403 |
| `test_audit_admin` | admin | GET /v1/audit | 200 (gov_http mock returns 200) |
| `test_audit_non_admin` | user | GET /v1/audit | 403 |
| `test_delete_user_admin` | admin | DELETE /v1/users/u1 | 202 |
| `test_delete_user_non_admin` | user | DELETE /v1/users/u1 | 403 |

Admin endpoint tests mock `app.state.gov_http` (the raw httpx client used to proxy audit/delete calls to governance).

### test_models.py

| Test | Expected |
|---|---|
| `test_list_models` | GET /v1/models → `{"object":"list","data":[...]}` |
| `test_me` | GET /v1/me → has `user_id`, `tenant_id`, `roles`, `rate_limit`, `pii_policy` keys |

## Docker Smoke Test (`tests/integration/test_smoke.py`)

Skipped automatically unless `INTEGRATION_TEST=1` is set. Reads `GATEWAY_BASE_URL` (default `http://localhost:8000`) and `JWT_SECRET` from environment. Uses `httpx.AsyncClient` directly—no ASGI tricks, no mocking.

Requires the proxy to be started in mock mode (`MOCK_PROVIDERS=true` or `OPENAI_API_KEY=mock`).

| Test | Assertion |
|---|---|
| `test_health` | GET /health → 200 |
| `test_unauthed_is_401` | POST /v1/chat/completions (no auth) → 401 |
| `test_models_list` | GET /v1/models with JWT → `object == "list"` |
| `test_chat_roundtrip` | POST /v1/chat/completions with JWT, benign message → 200, valid choices array |

## Makefile Changes

Add a `test-integration` target alongside the existing `test`:

```makefile
test-integration:
    INTEGRATION_TEST=1 pytest tests/integration/
```

## Dependencies

No new dependencies. `pytest-asyncio`, `pytest-httpx`, and `httpx` are already in `proxy/pyproject.toml` dev extras.
