---
title: "feat: LLM Governance Gateway"
type: feat
status: active
date: 2026-05-26
origin: docs/brainstorms/2026-05-26-llm-governance-gateway-brainstorm.md
deepened: 2026-05-26
gaps_resolved: 2026-05-26
gaps_doc: docs/gaps/2026-05-26-pre-build-gaps.md
---

# feat: LLM Governance Gateway

## Enhancement Summary

**Deepened on:** 2026-05-26
**Agents used:** security-sentinel, architecture-strategist, kieran-python-reviewer, performance-oracle, data-integrity-guardian, backend-architect, code-simplicity-reviewer, system-architect + 5 learned skills

### Critical Corrections (Breaking Changes to Original Plan)

1. **asyncio.gather ordering is wrong** — PII must complete before OPA (OPA needs `data_classification` from PII findings). Fix: run PII first, then `asyncio.gather(harm, opa)` concurrently. *(architecture-strategist, performance-oracle, backend-architect — unanimous)*
2. **API key plaintext storage** — must store `(prefix, bcrypt_hash)`, never raw key. *(security-sentinel)*
3. **Governance engine /inspect is unauthenticated** — add `X-Internal-Token` shared secret header. *(security-sentinel, backend-architect)*
4. **PII matched text stored in audit log** — COMPLIANCE VIOLATION. Store only metadata (entity type + offset), never the matched substring. *(data-integrity-guardian)*
5. **`get_event_loop()` deprecated in Python 3.10+** — use `asyncio.to_thread()` instead. *(kieran-python-reviewer)*
6. **Redis Lua `math.random()` produces duplicate members** — use INCR counter for unique member IDs. *(kieran-python-reviewer)*
7. **OPA deny + allow can both fire simultaneously** — block if `allow==false` OR `len(deny)>0`. *(security-sentinel)*
8. **OPA was being evaluated against the inferred provider, not the resolved one** — provider override / table lookup must happen in the proxy entry-point BEFORE `/inspect`, otherwise PHI-to-unapproved-provider policy is bypassable via `X-Gateway-Provider`. *(security-sentinel — gap review)*
9. **Bootstrap admin key "no admin row" check is re-triggerable** — delete admin row, restart, env var still present, bootstrap fires again. Use a dedicated `bootstrap_state` row whose presence is the idempotency guard. *(security-sentinel — gap review)*
10. **Postgres session vars leak across pooled connections** — `app.current_*` must be `RESET` on every connection checkout via a SQLAlchemy `checkout` event listener, before OPA-derived values are set. *(security-sentinel — gap review)*
11. **Partitioned table with no children rejects all INSERTs** — initial Alembic migration must pre-create the first 3 monthly `audit_log` partitions. *(data-integrity-guardian — gap review)*
12. **`audit_id` must be UUIDv7, not `gen_random_uuid()`** — keyset pagination requires monotonic ordering; random UUIDs break the cursor tiebreaker under concurrent writes. *(data-integrity-guardian — gap review)*
13. **`user_pseudonym_map` requires `tenant_id`** — without it, two tenants with the same `real_user_id` value collide in the same pseudonym row and one tenant's erasure request anonymizes another tenant's user. *(data-integrity-guardian — gap review)*

### Key Improvements

- **Simplified architecture**: Cut HarmClassifier ABC, registry.py, asyncio.Queue audit mode, `flag` action — ~40% fewer abstraction layers *(code-simplicity-reviewer)*
- **GovernancePipeline as middleware chain**: Each stage is `async (ctx: PipelineContext) -> PipelineContext`; ordered list enables explicit PII-before-OPA dependency *(backend-architect)*
- **Docker Compose healthcheck chain**: `migrate` service + `start_period: 30s` on governance + governance `/health` returns 503 until Presidio ready *(system-architect)*
- **Fly.io memory reality check**: `en_core_web_lg` = ~970MB; use `performance-1x` + `auto_stop_machines = true` + `en_core_web_sm` on Fly *(system-architect)*
- **In-process auth cache**: `cachetools.TTLCache` for API key lookup eliminates per-request Postgres hit *(performance-oracle)*
- **CORS explicit allowlist**: Required on both services; never wildcard *(learned: cors-security-explicit-allowlists)*

### New Considerations Discovered

- OPA sidecar needs `--watch` flag for policy hot-reload during development
- `EXPIRE` in Lua script must run unconditionally (not only on allow path), and add `ZREMRANGEBYSCORE` to evict stale entries
- Alembic `migrate` service with `condition: service_completed_successfully` required before governance engine starts
- FastAPI `/docs` must be disabled in production (enables only in dev/demo mode)
- `written_at` column needed in audit log (separate from `created_at`) to detect write lag
- Presidio `AnonymizerEngine()` also blocks event loop — needs `asyncio.to_thread()` treatment

---

## Design Principles (binding for all decisions)

1. **No third-party LLM gateway library.** Do not depend on LiteLLM, PortKey, or similar OSS gateways. Provider routing, adapters, and config parsing are owned in-tree. Reference these projects for design comparison only. Rationale: a governance gateway sits on the security boundary; every transitive dependency is a place where an unused-feature CVE becomes our compliance incident. Copy the small focused code we need; do not pull the kitchen sink.
2. **Configuration is code.** No manual key creation, tenant creation, or role grant. All identity and policy state flows from `config/tenants.yaml`, `config/users.yaml`, `config/models.yaml`, and `policies/*.rego`. Idempotent `make provision` reconciles these to Postgres and OPA data documents.
3. **Fail-closed on every governance boundary.** Auth fail, OPA timeout, governance unreachable, scope-resolution error → block, not pass-through.
4. **Audit log is metadata only.** Never store matched PII text, prompt content, model output, or anything that turns the log itself into a regulated data store.

---

## Pre-Build Gap Resolutions (2026-05-26)

Six gaps from `docs/gaps/2026-05-26-pre-build-gaps.md` were researched (5 parallel best-practices passes) and the load-bearing security/data decisions were adversarially reviewed (security-sentinel + data-integrity-guardian). The reviews produced material revisions to four of the six initial decisions. Below is the final, build-ready resolution for each gap.

### Gap 1 — User Provisioning IaC

**Schema (`config/tenants.yaml`):**
```yaml
tenants:
  - id: acme                          # slug; foreign key everywhere
    name: Acme Corp
    allowed_models: [gpt-4o, gpt-4o-mini, claude-sonnet-4-6]
    rate_limit:
      requests_per_minute: 500
      tokens_per_minute: 200000
    pii_action: redact                # redact | block | passthrough
    pii_redaction_notification: header_only   # header_only | silent
    default_provider: openai          # used only when model name is ambiguous
    contact_email: ops@acme.example
```

**Schema (`config/users.yaml`):**
```yaml
users:
  - id: alice@acme.example            # stable identifier; email is fine
    tenant_id: acme
    roles: [tenant_admin, tier2-access]
    initial_key: generate             # generate | none — provisioner directive
```

**Bootstrap admin key — revised after security review.** The naive "no admin row exists" check has a re-trigger-on-deletion attack (delete admin row → restart → bootstrap fires again from still-present env var). Replaced with an explicit one-time idempotency record:

```sql
CREATE TABLE bootstrap_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  bootstrap_completed_at TIMESTAMPTZ NOT NULL,
  bootstrap_token_fingerprint TEXT NOT NULL    -- sha256 of consumed token, for forensics
);
```

- On startup, gateway checks `SELECT EXISTS (SELECT 1 FROM bootstrap_state WHERE id = 1)`. If false AND `GATEWAY_BOOTSTRAP_TOKEN` env var is set, run bootstrap inside a single transaction: (a) insert admin API key (bcrypt of token), (b) insert the bootstrap_state row, (c) commit. Concurrency: a unique constraint on `bootstrap_state.id` + `INSERT … ON CONFLICT DO NOTHING` makes the second concurrent startup observe zero rows inserted and log "bootstrap skipped — admin already exists" rather than creating a duplicate admin.
- After bootstrap succeeds, the application calls `os.environ.pop("GATEWAY_BOOTSTRAP_TOKEN", None)` to remove the token from the process environment. This does not erase `/proc/<pid>/environ` (kernel-owned), but eliminates exposure in subsequent crash dumps and Sentry envelopes.
- Rotation: `make rotate-bootstrap` clears the `bootstrap_state` row, requires an operator to set a new `fly secret`, and restarts the pod. The previously-bootstrapped admin keys are not automatically revoked; the operator must do that explicitly.

**Key distribution.** `make provision` prints the generated key once to stdout with a "store this now, it will not be shown again" warning. The Makefile documents how to pipe into a secrets manager for production use; the portfolio demo uses stdout.

**Role assignment.** Roles are declared in `config/users.yaml`. The provisioner emits `policies/data/users.json` and `policies/data/tenants.json` as OPA data documents. OPA's existing `--watch` flag picks them up without restart. `users.yaml` is the single source of truth; the OPA data JSON files are derived artifacts and never edited by hand.

### Gap 2 — Provider Routing

**Hybrid resolution order** (first match wins):

1. **`X-Gateway-Provider` header**, only when the caller has OPA permission `gateway:provider_override:<provider>` (per-provider scoped — see security revision below). Header sets provider name only; never sets a base URL (SSRF defense).
2. **`config/models.yaml` explicit entry**, mapping `model_id → {provider, base_url, alias_of?}`. Handles aliases, private deployments, ambiguous model names. Schema:
   ```yaml
   models:
     - id: gpt-4o
       provider: openai
     - id: acme-tuned-v3
       provider: openai
       alias_of: gpt-4o-mini
     - id: mistral-large
       provider: mistral-cloud      # ambiguity resolution
   ```
3. **Model-name prefix inference**: `gpt-*`, `o1-*`, `o3-*`, `o4-*` → openai; `claude-*` → anthropic; `gemini-*` → gemini; `llama-*`/`mistral-*`/`phi-*`/`qwen-*` → ollama.
4. **Tenant default provider** from `tenants.yaml`. Used only when the prefix is unambiguous-but-unknown.
5. **Catch-all**: 400 `{"error": {"type": "invalid_request_error", "code": "model_not_found"}}` (matches OpenAI). Optional opt-in `routing.unknown_model_handler: {provider, base_url}` for self-hosted Ollama defaults.

**CRITICAL ordering fix from security review.** OPA must evaluate the **resolved** provider, not the inferred-from-model-name provider. The original plan called `/inspect` with `provider` inferred from model name, then resolved the override later in the proxy — meaning OPA's PHI-to-unapproved-provider check ran against the wrong provider. Fix: provider resolution moves to the proxy entry-point, **before** `/inspect`. The `provider` field in the `InspectRequest` is now always the final routed provider.

**`X-Gateway-Provider` permission scoping (security revision).** Use `gateway:provider_override:<provider_name>` (e.g., `gateway:provider_override:anthropic`), not a global `gateway:provider_override`. A compromised key with override to anthropic cannot pivot to gemini. When the header is present without the matching permission, the request routes normally AND an audit log entry is written with `routing_method: "override_denied"` and `attempted_provider: <value>` so probing is visible.

**`GET /v1/models` response.** OPA-filtered to the caller's effective set (intersect `tenants[id].allowed_models` with per-model OPA `allow_model` checks). Returns OpenAI Model object shape: `{id, object: "model", created, owned_by}`. Internal routing metadata (base URLs, alias targets) is never exposed.

**`routing_method` is never returned to the caller** (audit-log-only). Otherwise it leaks the override permission model via binary search.

### Gap 3 — Error Responses, Rate-Limit Headers, PII Notification

**Governance violation error schema** (extends OpenAI envelope; preserves SDK compatibility):
```json
{
  "error": {
    "message": "Request blocked by content policy. See violations[] for details.",
    "type": "invalid_request_error",
    "code": "policy_violation",
    "param": null,
    "violations": [
      {
        "policy": "pii-protection",
        "reason": "Input contains PII that cannot be forwarded to this provider.",
        "entity_types_detected": ["EMAIL_ADDRESS", "US_SSN"],
        "suggestions": ["Remove or pseudonymize PII before sending."],
        "documentation_url": "https://gateway.example/docs/policies/pii"
      }
    ],
    "approved_providers_for_classification": ["azure-openai-govcloud"],
    "required_roles": ["llm:unrestricted"]
  }
}
```

- `entity_types_detected` carries **types only**. Never offsets, matched substrings, or confidence scores.
- `required_roles` is included; **current roles are NOT echoed back** (would leak RBAC structure to anyone holding a key).
- `approved_providers_for_classification` is emitted only on `provider_not_allowed` / `data_classification_mismatch` violations; omitted for plain PII blocks.

**`Retry-After` (429 only).** Seconds until the oldest entry in the sliding window expires. Lua script returns this value (extends the script's return signature from `0|1` to `{allowed: 0|1, retry_after_seconds: int}`). Floor at 1 second. Emit both:
- `Retry-After: <int seconds>` (RFC 7231)
- `retry-after-ms: <int ms>` (matches OpenAI SDK retry handler exactly)

**Rate-limit headers on every response** (200 and 429). Mirror OpenAI's exact header names so the OpenAI SDK's built-in instrumentation works:
```
x-ratelimit-limit-requests: 500
x-ratelimit-remaining-requests: 357
x-ratelimit-reset-requests: 6s
```
Token-budget headers (`*-tokens`) are emitted only if/when token budgeting is added (out of v1 scope; namespace reserved).

**PII redaction notification — header by default, tenant opt-out.** When silent redaction occurs:
```
X-Gateway-Pii-Redacted: true
X-Gateway-Pii-Types: PERSON,EMAIL_ADDRESS
```
- **Default = `header_only`.** Silent redaction without notification breaks reproducibility — developers cannot debug why the model's response doesn't match their prompt.
- Tenants needing strict no-channel mode set `pii_redaction_notification: silent` in `tenants.yaml`.
- Never emit offsets, matched strings, or replacement tokens. Types only.
- No body field. Headers only — keeps the response body shape unchanged for OpenAI SDK clients.

### Gap 4 — `GET /v1/me` (Self-Service Visibility)

**Response:**
```json
{
  "user_id": "alice@acme.example",
  "tenant_id": "acme",
  "roles": ["tenant_admin", "tier2-access"],
  "allowed_models": ["gpt-4o", "gpt-4o-mini"],
  "rate_limit": {"limit": 500, "used": 143, "remaining": 357, "resets_at": "2026-05-26T14:32:00Z"},
  "pii_policy": {"action": "redact", "entities": ["PERSON", "EMAIL_ADDRESS", "US_SSN"]}
}
```

- `resets_at` as ISO 8601 (not Unix epoch — matches OpenAI; human-readable in logs).
- **Allowed models = enumerate-and-filter, NOT OPA partial evaluation.** The tenant's `allowed_models` is bounded (≤ ~20 entries); a loop of HTTP OPA queries against `authz/allow_model` per model is simple, correct on every policy change, and avoids partial-eval residual-policy complexity. Cache the entire `/v1/me` response for 30s in `cachetools.TTLCache` if latency becomes a concern.
- **Current rate-limit usage IS exposed.** GitHub, OpenAI, and Stripe all expose this; the "gaming the sliding window" concern is marginal because 429 responses already leak the boundary. Transparency reduces support load.

### Gap 5 — Mock Provider Mode

**Implementation:** `proxy/app/providers/mock.py` as a first-class `Provider` subclass. Pattern-matches on the `messages` content to dispatch to a canned scenario. This path exercises the full middleware stack (auth, rate limit, governance pipeline, audit log) — `respx`-style transport interception bypasses adapters and is unsuitable.

**Activation:** layered, env-flag takes precedence over sentinel key.
- `MOCK_PROVIDERS=true` (CI override, beats everything)
- `OPENAI_API_KEY=mock` (sentinel — Stripe `sk_test_*` convention)
- Resolved once at startup into `settings.mock_mode: bool`; never re-read per-request.

**Streaming mock:** async generator yields pre-chunked SSE lines wrapped in `httpx.AsyncByteStream`. `MOCK_STREAM_DELAY_MS=0` for pytest, `15` for demo so reviewers see visible token streaming.

**Test fixtures** (`tests/fixtures/mock_scenarios.py`, `@dataclass` instances):

| Scenario key | Trigger | Expected outcome |
|---|---|---|
| `clean_request` | "What is the capital of France?" | allow + full response, audit `decision=allow` |
| `pii_redact` | contains email or SSN pattern | redacted content forwarded, `decision=redact` |
| `phi_deny` | contains "patient diagnosis" + non-approved provider | OPA deny set, 403 returned, upstream **never called** |
| `prompt_injection` | "ignore previous instructions" | harm scanner blocks, 400, `decision=block` |
| `model_tier_deny` | tier1 user requesting `gpt-4o` | OPA model-tier deny, 403 |
| `rate_limit_exceed` | N+1 concurrent requests via `asyncio.gather` | exactly one 429 |

VCR (`vcrpy`) cassettes are dev-only, used for golden snapshots where exact token counts matter. CI never touches the network.

### Gap 6 — Audit Log Access, Retention, GDPR Erasure, Export

**Access model.** User-scoped by default; tenant admins see their tenant; platform admins see all. Two-layer enforcement:

1. **OPA at the endpoint** maps caller's `(user_id, tenant_id, roles)` to a scope enum: `SELF` | `TENANT` | `PLATFORM`.
2. **Postgres RLS** as data-layer backstop. The application validates the OPA-returned scope against an explicit allowlist **before** issuing `SET LOCAL`, then sets session vars per transaction:
   ```python
   ALLOWED_SCOPES = frozenset({"SELF", "TENANT", "PLATFORM"})
   if scope not in ALLOWED_SCOPES:
       raise HTTPException(403)                    # never trust OPA output for SET LOCAL
   ```

**Connection-pool reuse fix (security revision).** Session vars `app.current_*` MUST be reset at every connection checkout, **before** OPA-derived values are set. Use SQLAlchemy `AsyncEngine` `connect` event listener:
```python
@event.listens_for(engine.sync_engine, "checkout")
def reset_session_vars(dbapi_connection, *_):
    with dbapi_connection.cursor() as cur:
        cur.execute("RESET app.current_user_id; RESET app.current_tenant_id; RESET app.current_scope")
```

**RLS policy** (positive-match form; default deny):
```sql
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;   -- applies even to table owner
CREATE POLICY audit_read ON audit_log FOR SELECT
USING (
  current_setting('app.current_scope', true) = 'PLATFORM'
  OR (current_setting('app.current_scope', true) = 'TENANT'
      AND tenant_id = current_setting('app.current_tenant_id', true))
  OR user_id = current_setting('app.current_user_id', true)
);
REVOKE ALL ON audit_log FROM gateway_app;
GRANT SELECT, INSERT ON audit_log TO gateway_app;     -- gateway_app is NOT table owner; NOT superuser
```

**Retention — revised after data-integrity review.** Drop `pg_partman` (Fly.io Postgres extension availability unverified; one more dependency surface). Use **native declarative partitioning + a state table for recoverable detach-dump-drop**:

- **Initial migration** creates `audit_log` partitioned by RANGE and pre-creates the first 3 monthly partitions. (Partitioned table with zero children rejects all INSERTs — this is a cross-cutting bug in the original schema.)
- **`partition_archive_state`** table tracks the lifecycle: `partition_name, detached_at, dumped_at, row_count, s3_key, verified_at, dropped_at`. Each step records its completion; recovery is "find rows where `dumped_at IS NULL AND detached_at < now() - interval '1 hour'`".
- **Nightly maintenance** is a Fly.io scheduled job (or local `make rotate-partitions`) — no `pg_cron` dependency. Python script: create next month's partition, detach partitions older than 12 months, dump verified partitions to S3, drop verified partitions.
- **Default retention:** 1 year hot (SOC 2 evidence window), 6-year archive tier (HIPAA §164.316(b)(2)(i)).
- **Drift detection:** a `/health` sub-check queries `partition_archive_state` for stuck partitions; alerts if any partition has `detached_at > 48h ago AND dumped_at IS NULL`.

**GDPR right-to-erasure — schema revised after data-integrity review.**

```sql
CREATE TABLE user_pseudonym_map (
  pseudonym       TEXT PRIMARY KEY,           -- key on pseudonym, not real_user_id
  real_user_id    TEXT NOT NULL,
  tenant_id       TEXT NOT NULL,              -- REQUIRED: prevents cross-tenant collision
  algorithm       TEXT NOT NULL DEFAULT 'hmac-sha256-v1',
  rotation_id     INTEGER NOT NULL DEFAULT 1,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at      TIMESTAMPTZ,
  UNIQUE (real_user_id, tenant_id, rotation_id)
);

CREATE TABLE erasure_log (                     -- auditor-acceptable proof-of-erasure
  erasure_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id    TEXT NOT NULL,
  pseudonym    TEXT NOT NULL,
  requested_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ NOT NULL,
  requestor    TEXT NOT NULL,
  audit_row_count INTEGER NOT NULL            -- how many audit_log rows are now opaque
);
```

- **Pseudonym is deterministic HMAC-SHA256** of `(tenant_id, real_user_id, rotation_id)`, keyed by a server-side `PSEUDONYM_HMAC_KEY` (Fly secret). Deterministic so investigators can confirm coverage *before* erasure; key rotation (bump `rotation_id`) breaks long-term linkage.
- **Audit log stores ONLY the pseudonym** in its `user_id` column. The real user identifier never lands in `audit_log`.
- **Erasure workflow:**
  1. Set `deleted_at = now()` on the map row.
  2. Overwrite `real_user_id` with sentinel `'[ERASED]'` (so the map row itself contains no PII).
  3. INSERT into `erasure_log` with completion timestamp + audit row count.
  4. Return 202 with the `erasure_id`.
- **Exact timestamps in audit log are PRESERVED.** Hour-bucketing destroys HIPAA forensic value; the correlation residual it would prevent is marginal. Mitigation for correlation: pseudonym rotation (`rotation_id` bump on a documented schedule).
- **Migration safety:** the pseudonym map is introduced from day 1. No backfill needed because audit_log will only ever contain pseudonyms.

**Export — revised after data-integrity review.** Replace server-side cursor with **HTTP-layer keyset pagination**:

- `GET /v1/audit/export?format=jsonl&after_created_at=<ts>&after_audit_id=<uuid>&until=<ts>`
- Query: `WHERE (created_at, audit_id) > ($1, $2) AND created_at <= $3 ORDER BY created_at, audit_id LIMIT 500`
- **`audit_id` is UUIDv7** (time-ordered), not `gen_random_uuid()`. This makes `ORDER BY audit_id` align with `ORDER BY created_at` to millisecond precision and removes the tiebreaker collision class entirely.
- The `created_at <= $3` upper bound is critical for partition pruning — without it the planner sequential-scans every partition.
- Response: `Content-Type: application/x-ndjson`, `Transfer-Encoding: chunked`, plus a `Link: <…>; rel="next"` header carrying the keyset of the last emitted row.
- No cursor leak risk: stateless at the DB; client resume is just replaying the last received `Link`.
- v1 ships JSONL only. CSV deferred — column flattening of JSONB requires product decisions (which subfields to promote).

---

## Overview

An enterprise-grade LLM governance gateway built as a portfolio project targeting AI platform engineer and MLOps roles. The system intercepts all LLM requests, runs a governance pipeline (PII detection, compliance policy enforcement, harm classification), and produces a full audit trail — all before forwarding approved requests to the target LLM provider.

Two FastAPI services with a clean ext_authz-style API contract between them: a **reference proxy** (OpenAI-compatible surface, swappable by design) and a **governance engine** (PII + policy + audit). Any future proxy — Envoy, nginx+Lua, Rust — can replace the reference proxy by calling the same governance engine API.

## Problem Statement

Enterprise teams deploying LLMs face three hard problems: sensitive data (PII, PHI) leaking to external providers, no enforcement of who can use which models and under what conditions, and no auditable record of what was sent and what policy decisions were made. Existing solutions (LiteLLM guardrails, AWS Bedrock guardrails) are tightly coupled to specific platforms. This project demonstrates a portable, provider-agnostic governance layer as a standalone service.

## Architecture

```
Client App (OpenAI SDK, curl, etc.)
    │  OpenAI-compatible API
    ▼
Reference Proxy :8741 (FastAPI)       ← swappable: Envoy / Rust / nginx
    │  1. Auth (API key [hashed lookup] or JWT [HS256 pinned])
    │  2. Rate limit check (Redis Lua, keyed on user_id from CallerContext)
    │  3. POST /inspect → Governance Engine (X-Internal-Token required)
    ▼
Governance Engine :8742 (FastAPI)
    │  Middleware-style pipeline (PipelineContext passed through each stage)
    ├── Stage 1: PII Detector (presidio-analyzer, asyncio.to_thread)
    ├── Stage 2: asyncio.gather → Harm Scanner + OPA Policy Evaluator
    └── Stage 3: Audit Logger (Postgres, BackgroundTasks — stores metadata only, not matched text)
    │
    ▼ {decision: allow|block|redact, redacted_messages?, violations?, audit_id}
    │
Reference Proxy (if allowed)
    │
    ▼
Provider Router  (match/case dispatch, not registry pattern)
    ├── openai     → OpenAI adapter
    ├── anthropic  → Anthropic adapter (messages format translation)
    ├── gemini     → Gemini adapter (Vertex AI REST)
    ├── ollama     → Ollama adapter (OpenAI-compat, local base URL)
    └── *          → Generic OAI-compat (pass-through with base URL config)
    │
    ▼ stream or JSON response
Client App
```

**Governance Engine /inspect contract:**
```
POST /inspect
Headers: X-Internal-Token: <shared-secret>  ← REQUIRED, validated on every call
{
  "request_id": "uuid",
  "phase": "request",              ← "request" | "response" (future: response-side inspection)
  "provider": "openai",
  "model": "gpt-4o",
  "messages": [{"role": "user", "content": "..."}],
  "caller": {"user_id": "u123", "tenant_id": "t1", "roles": ["analyst"]},
  "stream": false
}

Response:
{
  "decision": "allow" | "block" | "redact",
  "redacted_messages": [...],        // only when decision == "redact"
  "violations": [{"policy": "...", "reason": "..."}],
  "pii_findings": [                  // metadata ONLY — no matched text
    {"entity": "PERSON", "start": 5, "end": 12, "score": 0.95}
  ],
  "data_classification": ["PII"],    // derived from pii_findings, passed to OPA
  "harm_score": 0.12,
  "audit_id": "uuid"                 // eventually consistent — write is async
}
```

**Research Insights — Architecture:**

- **`phase` discriminator**: Adding `phase: "request" | "response"` now prepares the contract for response-side inspection without a breaking change. OPA policies branch on `phase`. *(architecture-strategist)*
- **Pipeline ordering**: PII must complete first (provides `data_classification` for OPA). Then harm and OPA run concurrently via `asyncio.gather`. The `PipelineContext` dataclass carries all intermediate state. *(architecture-strategist, backend-architect, performance-oracle — unanimous)*
- **Provider dispatch**: Use `match provider: case "openai": ...` in the router — eliminates registry abstraction for a portfolio that never needs runtime-pluggable providers. *(code-simplicity-reviewer)*
- **CallerContext shape**: Flat dataclass with all fields — OPA performs better with flat input documents and nested objects create fragile JSON path coupling in Rego. *(architecture-strategist)*

## Resolved Design Decisions (from brainstorm)

See brainstorm: `docs/brainstorms/2026-05-26-llm-governance-gateway-brainstorm.md`

- **Sidecar governance service**: proxy and governance engine are separate FastAPI services with a clean HTTP contract — makes proxy fully swappable
- **FastAPI for both services**: consistent stack, async, auto OpenAPI docs at `/docs` (disabled in production)
- **OPA for access/compliance, YAML for pipeline config**: OPA handles conditional model-access and data-classification logic; YAML handles detector thresholds and routing
- **Presidio for PII**: Microsoft-maintained, production-proven, supports custom recognizers
- **OpenAI-compatible proxy API surface**: existing client code works without modification
- **uv for all Python deps** (per CLAUDE.md)
- **Ports**: `8741` (proxy), `8742` (governance engine) — non-default
- **Audit UI**: FastAPI `/docs` Swagger UI (dev/demo only; disabled in production)
- **Rate limiting**: per API key + per user, Redis sliding window keyed on `user_id` from CallerContext
- **Harm classification**: rules-only initially (llm-guard), simplified to a plain function (no ABC)
- **Auth**: API keys stored as `(prefix, bcrypt_hash)` + JWT bearer pinned to HS256; both resolve to `caller` context
- **No LiteLLM / OSS gateway dependency** (see Design Principles): provider routing and adapters are owned in-tree
- **IaC provisioning**: `config/tenants.yaml`, `config/users.yaml`, `config/models.yaml` reconciled by `make provision` (idempotent)
- **Bootstrap admin key**: env-injected one-time token gated by a `bootstrap_state` table (not "no admin row" check)
- **Provider routing**: hybrid override-header (permissioned) → `models.yaml` → prefix inference → tenant default → 400
- **Mock provider**: first-class `proxy/app/providers/mock.py` adapter; activated by `MOCK_PROVIDERS=true` or `OPENAI_API_KEY=mock`
- **Audit access**: user-scoped default; OPA-derived scope (`SELF`|`TENANT`|`PLATFORM`) + Postgres RLS backstop with allowlist validation
- **GDPR erasure**: HMAC-SHA256 pseudonyms with `tenant_id` namespace + `erasure_log` proof table; exact timestamps preserved
- **Audit export**: HTTP-layer keyset pagination on `(created_at, audit_id)` with UUIDv7 `audit_id`; JSONL streaming, CSV deferred
- **Retention**: native declarative partitioning + `partition_archive_state` recoverable detach-dump-drop; no `pg_partman`/`pg_cron`

## Technical Approach

### Tech Stack

| Component | Technology | Notes |
|---|---|---|
| Reference Proxy | Python 3.12, FastAPI, uvicorn | Port 8741 |
| Governance Engine | Python 3.12, FastAPI, uvicorn | Port 8742 |
| PII Detection | `presidio-analyzer` + `en_core_web_lg` (local) / `en_core_web_sm` (Fly.io) | `asyncio.to_thread()` singleton |
| Harm Scanner | `llm-guard` (PromptInjectionScanner + BanTopicsScanner) | Plain function, no ABC |
| Policy Engine | OPA sidecar (openpolicyagent/opa:latest-static --watch) | Port 8181 internal |
| Audit Storage | Postgres + SQLModel + asyncpg, native declarative monthly RANGE partitions | INSERT+SELECT app role, RLS enforced |
| Audit ID | UUIDv7 via `uuid7` package (small, focused; or hand-rolled per Design Principles) | Monotonic ordering for keyset pagination |
| Partition Lifecycle | Native Postgres + `partition_archive_state` table + Fly scheduled job | No `pg_partman`, no `pg_cron` dependency |
| Pseudonymization | HMAC-SHA256 with server-side key (`PSEUDONYM_HMAC_KEY` Fly secret) | Deterministic; rotation via `rotation_id` |
| Rate Limiting | Redis 7 via redis-py async | Lua script with INCR counter for unique members |
| Auth caching | `cachetools.TTLCache` (in-process, 60s TTL) | Eliminates per-request Postgres lookup |
| HTTP Client | httpx AsyncClient (explicit connection limits) | Reused per app lifespan |
| Package Management | uv only (no pip anywhere, including Dockerfiles) | Per CLAUDE.md |
| Containers | Docker Compose | `make up` → all services detached |
| Hosting | Fly.io (performance-1x, auto_stop_machines=true) | Docker Compose for local |

### Project Structure

```
ai-gateway/
├── Makefile                         # up, down, restart, logs, status, migrate, lint, test, opa-test, provision, rotate-bootstrap, rotate-partitions, demo
├── .envrc                           # direnv: API keys, DB URL, JWT secret, internal token, OPA URL, GATEWAY_BOOTSTRAP_TOKEN, PSEUDONYM_HMAC_KEY
├── .envrc.example                   # Shipped template; .envrc is gitignored
├── .gitignore                       # includes .envrc
├── docker-compose.yml               # postgres, redis, opa, migrate (one-shot), proxy, governance
├── governance.yaml                  # pipeline config: detectors, thresholds, routing
├── config/                          # IaC: declarative tenant/user/model state — single source of truth
│   ├── tenants.yaml                 # Tenant definitions (allowed_models, rate_limit, pii_action, default_provider)
│   ├── users.yaml                   # User definitions (tenant_id, roles, initial_key directive)
│   └── models.yaml                  # Explicit model → provider routing table (overrides prefix inference)
├── scripts/
│   ├── provision.py                 # Reconciles config/*.yaml → Postgres + OPA data documents (idempotent)
│   ├── rotate_partitions.py         # Nightly: create next partition, detach >12mo, dump to S3, drop verified
│   └── pre-commit-security.sh       # Block secrets in commits
├── policies/                        # OPA .rego files (versioned in git)
│   ├── llm/
│   │   ├── authz.rego               # model access + data classification enforcement
│   │   ├── authz_test.rego          # opa test suite (includes deny+allow coexistence)
│   │   ├── audit_scope.rego         # SELF | TENANT | PLATFORM mapping from caller context
│   │   └── provider_override.rego   # gateway:provider_override:<provider> permission check
│   ├── data/                        # Derived artifacts — generated by scripts/provision.py
│   │   ├── users.json
│   │   └── tenants.json
│   └── .manifest                    # OPA bundle manifest
├── proxy/                           # Reference proxy service
│   ├── pyproject.toml
│   ├── Dockerfile
│   └── app/
│       ├── main.py                  # FastAPI app, lifespan, routes, CORS middleware, /v1/me, /v1/audit
│       ├── config.py                # Settings (env resolution; mock_mode resolved once at startup)
│       ├── auth.py                  # API key (bcrypt hash lookup + TTLCache) + JWT (HS256 pinned)
│       ├── bootstrap.py             # First-startup admin key bootstrap via bootstrap_state table
│       ├── rate_limit.py            # Redis sliding window (Lua: INCR + ZREMRANGEBYSCORE + Retry-After return)
│       ├── routing.py               # Resolve provider: header → models.yaml → prefix → tenant default → 400
│       ├── governance_client.py     # /inspect caller (post-routing-resolution provider); X-Internal-Token; tenacity retry
│       ├── headers.py               # Rate-limit headers, PII redaction headers, error-envelope builders
│       ├── providers/
│       │   ├── openai.py            # Reference implementation
│       │   ├── anthropic.py
│       │   ├── gemini.py
│       │   ├── ollama.py
│       │   ├── generic.py           # OpenAI-compatible pass-through
│       │   └── mock.py              # Pattern-matched canned scenarios; SSE generator for streaming
│       └── models.py                # Pydantic request/response models, CallerContext (flat dataclass)
├── governance/                      # Governance engine service
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── alembic/
│   ├── alembic.ini
│   └── app/
│       ├── main.py                  # FastAPI app, lifespan, /inspect route, X-Internal-Token validation
│       ├── db.py                    # AsyncEngine + checkout listener (RESET app.current_* session vars)
│       ├── pipeline.py              # Middleware chain: [pii_stage, harm_opa_stage]
│       ├── context.py               # PipelineContext dataclass (carries all intermediate state)
│       ├── pii.py                   # PIIDetector module-level singleton, asyncio.to_thread()
│       ├── harm.py                  # harm_scan() plain function, llm-guard scanners
│       ├── opa.py                   # OPA HTTP client, fail-closed, deny+allow check
│       ├── audit.py                 # write_audit() coroutine; pseudonymize user_id via HMAC; metadata-only
│       ├── pseudonym.py             # HMAC-SHA256 pseudonymization; rotation_id; PSEUDONYM_HMAC_KEY
│       ├── audit_export.py          # GET /v1/audit + keyset-paginated /v1/audit/export (JSONL)
│       ├── retention.py             # Detach-dump-drop coordinator; reads partition_archive_state
│       └── models.py                # InspectRequest, InspectResponse, AuditExportCursor Pydantic models
├── tests/
│   └── fixtures/
│       ├── mock_scenarios.py        # @dataclass fixtures for the 6 canned scenarios
│       └── cassettes/               # vcrpy recordings — dev-only, gated by RECORD_CASSETTES=true
└── docs/
    ├── brainstorms/
    ├── plans/
    └── gaps/
```

**Research Insights — Simplification (code-simplicity-reviewer):**
- Removed: `pii/`, `harm/`, `policy/`, `audit/` subdirectories → flat module files
- Removed: `HarmClassifier` ABC, `registry.py`, `HarmClassifier` base class — one plain `harm_scan()` function
- Removed: asyncio.Queue batch audit mode — `BackgroundTasks` is sufficient for portfolio load
- Removed: `flag` action from per-entity config — redact-or-block is complete
- `GovernancePipeline` → `pipeline.py` with an ordered list of stage functions; no class needed
- `ProviderAdapter` ABC → `match/case` dispatch in `proxy/app/main.py`

### Key Implementation Patterns

**PIIDetector — module-level singleton, asyncio.to_thread (Python 3.12 idiomatic):**
```python
# governance/app/pii.py
import asyncio
from presidio_analyzer import AnalyzerEngine, RecognizerResult
from presidio_anonymizer import AnonymizerEngine, AnonymizerResult

_analyzer: AnalyzerEngine | None = None
_anonymizer: AnonymizerEngine | None = None
_init_lock = asyncio.Lock()

async def initialize() -> None:
    global _analyzer, _anonymizer
    async with _init_lock:
        if _analyzer is not None:
            return
        # asyncio.to_thread is idiomatic Python 3.9+; replaces deprecated get_event_loop()
        _analyzer = await asyncio.to_thread(AnalyzerEngine)
        _anonymizer = await asyncio.to_thread(AnonymizerEngine)

async def scan(text: str) -> list[RecognizerResult]:
    assert _analyzer is not None, "PIIDetector not initialized"
    return await asyncio.to_thread(_analyzer.analyze, text=text, language="en")

async def redact(text: str, findings: list[RecognizerResult]) -> str:
    assert _anonymizer is not None, "PIIDetector not initialized"
    result: AnonymizerResult = await asyncio.to_thread(
        _anonymizer.anonymize, text=text, analyzer_results=findings
    )
    return result.text
```

**Research Insights — Python patterns (kieran-python-reviewer):**
- `get_event_loop()` is deprecated in Python 3.10+ and raises in future versions. Use `asyncio.to_thread()` (available since 3.9) for all CPU-bound blocking calls.
- Both `AnalyzerEngine()` and `AnonymizerEngine()` are CPU-bound at construction — both need `asyncio.to_thread()`, not just the analyzer.
- `asyncio.Lock()` on the class/module level prevents concurrent re-initialization race.
- `run_in_threadpool` (Starlette import) is equivalent but less idiomatic than `asyncio.to_thread` in modern Python.

**Pipeline — middleware chain with explicit PII-before-OPA ordering:**
```python
# governance/app/pipeline.py
import asyncio
from governance.app.context import PipelineContext
from governance.app import pii, harm, opa, audit

async def pii_stage(ctx: PipelineContext) -> PipelineContext:
    findings = await pii.scan(ctx.text)
    ctx.pii_findings = findings
    ctx.data_classification = _classify(findings)  # ["PII", "PHI", ...]
    if ctx.config.pii.action == "block" and findings:
        ctx.decision = "block"
        ctx.violations.append({"policy": "pii-detected", "reason": "PII blocked per policy"})
    elif ctx.config.pii.action == "redact" and findings:
        ctx.redacted_text = await pii.redact(ctx.text, findings)
        ctx.decision = "redact"
    return ctx

async def harm_opa_stage(ctx: PipelineContext) -> PipelineContext:
    # PII must complete first (provides data_classification for OPA)
    # Harm and OPA are independent — run concurrently
    harm_result, opa_result = await asyncio.gather(
        harm.scan(ctx.text),
        opa.evaluate(ctx.to_opa_input()),  # includes data_classification from pii_stage
    )
    ctx.harm_score = harm_result.score
    if harm_result.score >= ctx.config.harm.threshold:
        ctx.decision = "block"
        ctx.violations.append({"policy": "harm-threshold", "reason": "Harm score exceeded"})
    # Block if OPA denies OR if deny set is non-empty (both conditions must be checked)
    if not opa_result.allow or opa_result.deny:
        ctx.decision = "block"
        ctx.violations.extend([{"policy": v, "reason": r} for v, r in opa_result.deny])
    return ctx

PIPELINE: list[callable] = [pii_stage, harm_opa_stage]

async def run(ctx: PipelineContext) -> PipelineContext:
    for stage in PIPELINE:
        ctx = await stage(ctx)
        if ctx.decision == "block":
            break  # Short-circuit on block, but audit still fires
    return ctx
```

**OPA client — EVALSHA, explicit exception handling, deny+allow check:**
```python
# governance/app/opa.py
from dataclasses import dataclass, field
import httpx

@dataclass
class OPAResult:
    allow: bool = False
    deny: list[tuple[str, str]] = field(default_factory=list)  # (policy, reason)
    reason: str = ""

_script_sha: str | None = None  # Loaded at startup via SCRIPT LOAD if using Lua (OPA uses HTTP, not Lua)

async def evaluate(client: httpx.AsyncClient, input_doc: dict) -> OPAResult:
    try:
        resp = await client.post(
            "/v1/data/llm/authz",
            json={"input": input_doc},
            timeout=0.05,  # 50ms hard timeout
        )
        resp.raise_for_status()
        result: dict = resp.json().get("result", {})
        if "allow" not in result:
            # OPA returns {} when policy path doesn't exist — treat as deny
            return OPAResult(allow=False, reason="missing-result")
        return OPAResult(
            allow=result.get("allow", False),
            deny=[(v, "") for v in result.get("deny", [])],
        )
    except httpx.TimeoutException:
        return OPAResult(allow=False, reason="opa-timeout")
    except httpx.HTTPError:
        return OPAResult(allow=False, reason="opa-unavailable")
    # Do NOT catch Exception broadly — let programming errors surface
```

**Research Insights — OPA (architecture-strategist):**
- OPA returns `{}` (HTTP 200) when a policy path doesn't exist — always check for the key's presence, not just truthiness
- `deny` is a set comprehension in Rego — can be non-empty even when `allow = true`. Must block if either condition is true.
- Store the Lua EVALSHA for the Redis rate limiter at startup via `SCRIPT LOAD`; never re-send the script body on every request under load.

**Redis rate limit — corrected Lua script:**
```lua
-- proxy/app/rate_limit.py
RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
-- Evict stale entries (missing from original design)
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCOUNT', key, now - window, now)
if count >= limit then return 0 end
-- Use INCR for unique member ID (math.random() produces duplicates under concurrency)
local seq = redis.call('INCR', key .. ':seq')
redis.call('ZADD', key, now, now .. ':' .. seq)
-- EXPIRE runs unconditionally (was conditional on allow path in original)
redis.call('EXPIRE', key, window)
redis.call('EXPIRE', key .. ':seq', window)
return 1
"""
```

**Rate limiting — key on CallerContext, not headers:**
```python
# proxy/app/rate_limit.py
# NEVER key on X-Forwarded-For or X-Real-IP — trivially spoofed
# Key on validated user_id from CallerContext (post-auth identity)
async def check_rate_limit(redis: Redis, ctx: CallerContext, config: RateLimitConfig) -> bool:
    now_ms = int(time.time() * 1000)
    key_user = f"rl:user:{ctx.user_id}"
    key_key = f"rl:key:{ctx.api_key_prefix}"
    # Both checks must pass; use EVALSHA (SHA loaded at startup)
    user_ok = await redis.evalsha(_script_sha, 1, key_user,
                                  config.per_user_limit, config.window_ms, now_ms)
    key_ok = await redis.evalsha(_script_sha, 1, key_key,
                                 config.per_key_limit, config.window_ms, now_ms)
    return bool(user_ok) and bool(key_ok)
```

**Auth — bcrypt hashed keys, in-process TTL cache:**
```python
# proxy/app/auth.py
import bcrypt
import secrets
from cachetools import TTLCache

_cache: TTLCache[str, CallerContext] = TTLCache(maxsize=1000, ttl=60)

async def validate_api_key(raw_key: str, db: AsyncSession) -> CallerContext:
    prefix = raw_key[:8]
    if prefix in _cache:
        return _cache[prefix]
    # Fetch by prefix (stored in plaintext), verify hash
    record = await db.scalar(select(APIKey).where(APIKey.prefix == prefix))
    if not record or not bcrypt.checkpw(raw_key.encode(), record.hash.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")
    ctx = CallerContext(user_id=record.user_id, tenant_id=record.tenant_id, roles=record.roles,
                        api_key_prefix=prefix)
    _cache[prefix] = ctx
    return ctx

def validate_jwt(token: str, secret: str) -> CallerContext:
    import jwt
    # Pin algorithm explicitly — prevents algorithm confusion attacks (none, RS256 confusion)
    payload = jwt.decode(token, secret, algorithms=["HS256"])
    return CallerContext(user_id=payload["sub"], tenant_id=payload["tenant_id"],
                         roles=payload.get("roles", []))
```

**Inter-service authentication:**
```python
# governance/app/main.py
from fastapi import Security, HTTPException
from fastapi.security import APIKeyHeader

internal_token_header = APIKeyHeader(name="X-Internal-Token")

async def require_internal_token(token: str = Security(internal_token_header)) -> None:
    import secrets
    expected = settings.governance_internal_token
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid internal token")

# Apply to /inspect route
@app.post("/inspect", dependencies=[Depends(require_internal_token)])
async def inspect(...): ...
```

**Research Insights — Inter-service auth (backend-architect):**
- `secrets.compare_digest` is constant-time — prevents timing attacks on the token comparison
- The token goes in `.envrc` as `GOVERNANCE_INTERNAL_TOKEN` and in Docker Compose as an environment variable passed to both services
- This is directly upgradeable to mTLS later without changing the governance engine's interface

**Audit log — metadata only, never matched text:**
```python
# governance/app/audit.py
async def write_audit(session_factory, entry: AuditEntry) -> None:
    try:
        async with session_factory() as session:
            session.add(AuditLog(
                audit_id=entry.audit_id,
                request_id=entry.request_id,
                user_id=entry.caller.user_id,    # pseudonymized if needed
                tenant_id=entry.caller.tenant_id,
                provider=entry.provider,
                model=entry.model,
                # ONLY metadata — never store matched text (compliance requirement)
                pii_findings=[
                    {"type": f.entity_type, "start": f.start, "end": f.end, "score": f.score}
                    for f in entry.pii_findings
                ],
                violations=entry.violations,
                harm_score=entry.harm_score,
                decision=entry.decision,
                latency_ms=entry.latency_ms,
                created_at=datetime.utcnow(),
                written_at=datetime.utcnow(),   # separate from created_at for write-lag detection
            ))
            await session.commit()
    except Exception as e:
        logger.error(f"Audit write failed for audit_id={entry.audit_id}: {e}")
        # Request already completed — log and continue, never block
```

**Research Insights — Data integrity (data-integrity-guardian, learned: alembic-migration):**
- `pii_findings` must store ONLY `{type, start, end, score}` — never the matched substring. Storing matched text makes the audit log itself a PII data store subject to breach notification.
- `written_at` column (separate from `created_at`) enables detecting write lag — the gap is an operational signal for async durability risk.
- Alembic migration must set `fillfactor=100` storage parameter (audit rows never update).
- Add `CHECK (decision IN ('allowed','blocked','redacted'))` constraint in the migration.

### Streaming Governance Model

Response-side governance is the key design decision the spec doesn't resolve. Chosen approach:

| Phase | Timing | What's scanned | Latency impact |
|---|---|---|---|
| Pre-call | Synchronous, blocks request | Full request: PII (stage 1) → harm+OPA (stage 2) | +10-18ms P99 |
| During stream | Per-SSE-chunk, non-blocking | Regex-only: obvious PII patterns, injection phrases | <1ms per chunk |
| Post-call | AsyncIO background task | Full response: PII + harm in response content | Zero (async) |

If a chunk-level violation is detected mid-stream, the proxy terminates the upstream connection and sends an SSE error event (`data: {"error": "content-policy-violation"}`). The partial response is logged.

**Research Insight — Streaming (security-sentinel):**
- For HIPAA response-side enforcement: clients receive streaming chunks before post-call inspection completes. If content containing PHI is streamed and the client disconnects, the post-call block is meaningless. Document explicitly: current architecture is insufficient for HIPAA response-side compliance without buffering. Add a `strict_response_inspection: true` config option that buffers the full response (with documented latency tradeoff).

### OPA Policies (Rego)

```rego
# policies/llm/authz.rego
package llm.authz

default allow = false
default redact_pii = false

model_tiers := {"gpt-4o": "tier2", "gpt-3.5-turbo": "tier1", "claude-opus-4-5": "tier2"}

allow {
    tier := model_tiers[input.request.model]
    tier == "tier1"
}

allow {
    tier := model_tiers[input.request.model]
    tier == "tier2"
    "tier2-access" in input.user.roles
}

# Block PHI to non-approved providers — deny is a SET (can coexist with allow=true)
deny[msg] {
    "PHI" in input.request.data_classification
    not input.request.provider in {"azure-openai", "bedrock"}
    msg := "PHI cannot be sent to unapproved external providers"
}

redact_pii {
    count(input.request.pii_findings) > 0
    input.pipeline.pii.action == "redact"
}
```

**Research Insights — OPA (security-sentinel):**
- `deny` and `allow` can both be true simultaneously. The pipeline must check: `block if not allow OR len(deny) > 0`. This is not covered by the original plan.
- Add `authz_test.rego` with a test case that verifies: tier2 model + PHI + non-approved provider + user with tier2-access role → `allow=true` but `deny` non-empty → pipeline blocks.

### Pipeline Config (YAML)

```yaml
# governance.yaml
pipeline:
  pii:
    enabled: true
    entities: [PERSON, EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD, US_SSN, PHI, LOCATION]
    action: redact          # redact | block  (removed: flag — YAGNI)
    timeout_ms: 100         # fail-closed on timeout
  harm:
    enabled: true
    classifier: rules       # rules only (pluggable in future via harm.py module swap)
    threshold: 0.75
    action: block
  opa:
    enabled: true
    url: http://opa:8181    # Docker Compose; Fly.io: http://opa.internal:8181
    policy_path: llm/authz
    timeout_ms: 50
    fail_open: false        # fail-closed by default
  audit:
    enabled: true
    mode: background        # background only (removed: queue mode — YAGNI)
    write_failure: proceed  # proceed | block
  streaming:
    strict_response_inspection: false  # true = buffer full response (HIPAA compliance)
```

## Implementation Phases

### Phase 1: Project Foundation

**Deliverables:**
- Monorepo layout: `proxy/`, `governance/`, `policies/`, `config/`, `scripts/`, `tests/`, `docs/`
- `docker-compose.yml`: postgres (5432 internal), redis (6379 internal), opa (8181 internal), `migrate` one-shot service, proxy (:8741), governance (:8742)
- Top-level `Makefile` with `up`, `down`, `restart`, `logs`, `status`, `migrate`, `lint`, `test`, `opa-test`, `provision`, `rotate-bootstrap`, `rotate-partitions`, `demo` targets
- `.envrc` with direnv: provider API keys, JWT secret, `GOVERNANCE_INTERNAL_TOKEN`, `GATEWAY_BOOTSTRAP_TOKEN`, `PSEUDONYM_HMAC_KEY`, DB URL, OPA URL (`SPACY_MODEL=en_core_web_lg`)
- `.envrc.example` shipped; `.envrc` in `.gitignore`
- `config/tenants.yaml`, `config/users.yaml`, `config/models.yaml` with at least one tenant + one admin + one tier1 + one tier2 user, and a small model routing table
- `scripts/provision.py`: parses YAML, hashes initial keys, writes API key rows, emits `policies/data/users.json` + `policies/data/tenants.json` for OPA (idempotent — safe to re-run); prints generated keys once to stdout
- `pyproject.toml` for each service: Python 3.12, hatchling, ruff (line-length 100), pyright (standard), pytest-asyncio (asyncio_mode=auto)
- Base FastAPI apps with lifespan context managers and health check `GET /health` (returns 503 until all dependencies initialized; governance also checks `partition_archive_state` for stuck partitions and surfaces "degraded")
- CORS middleware with explicit `allow_origins` list on both services
- Pre-commit security hook
- Max body size middleware: hard limit of 1MB on both services

**Research Insights — Docker Compose startup chain (system-architect):**

```yaml
# docker-compose.yml (healthcheck chain)
services:
  postgres:
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $POSTGRES_USER -d $POSTGRES_DB"]
      interval: 5s
      timeout: 3s
      retries: 10

  redis:
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  opa:
    image: openpolicyagent/opa:latest-static
    command: ["run", "--server", "--addr=0.0.0.0:8181", "--watch", "/policies"]
    # --watch enables policy hot-reload during development
    volumes: ["./policies:/policies:ro"]
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8181/health || exit 1"]
      interval: 5s
      timeout: 3s
      retries: 10
      start_period: 5s

  migrate:
    build: ./governance
    command: ["uv", "run", "alembic", "upgrade", "head"]
    depends_on:
      postgres:
        condition: service_healthy
    restart: "no"

  governance:
    depends_on:
      migrate:
        condition: service_completed_successfully
      opa:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8742/health || exit 1"]
      interval: 10s
      timeout: 8s
      retries: 15
      start_period: 30s   # Presidio + spaCy en_core_web_lg takes 10-20s cold

  proxy:
    depends_on:
      redis:
        condition: service_healthy
      governance:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8741/health || exit 1"]
      interval: 5s
      timeout: 3s
      retries: 10
```

Key details:
- `start_period: 30s` on governance: without this, Docker counts the spaCy load period as failures and may kill the container.
- `migrate` service with `condition: service_completed_successfully`: migrations run after Postgres is healthy but before governance starts.
- Governance `/health` returns 503 until `pii_detector` is initialized in lifespan.
- OPA `--watch` flag enables policy hot-reload when `.rego` files change (development only; remove for production).

**Files to create:**
- `Makefile`
- `docker-compose.yml`
- `.envrc`, `.envrc.example` (add `.envrc` to `.gitignore`)
- `governance.yaml`
- `config/tenants.yaml`, `config/users.yaml`, `config/models.yaml`
- `scripts/provision.py`, `scripts/pre-commit-security.sh`
- `proxy/pyproject.toml`, `proxy/Dockerfile`, `proxy/app/main.py`, `proxy/app/config.py`
- `governance/pyproject.toml`, `governance/Dockerfile`, `governance/app/main.py`
- `policies/llm/authz.rego`, `policies/llm/audit_scope.rego`, `policies/llm/provider_override.rego`, `policies/.manifest`

**Success criteria:**
- [ ] `make up` starts all services detached, no errors
- [ ] `make provision` reconciles `config/*.yaml` → Postgres + `policies/data/*.json`; idempotent (second run prints "no changes")
- [ ] `make provision` prints generated key(s) once to stdout with "store this now" warning
- [ ] `curl localhost:8741/health` returns 503 (proxy up, governance not yet ready) → eventually 200
- [ ] `curl localhost:8742/health` returns 503 until Presidio loaded → then 200
- [ ] OPA bundle loads (`curl localhost:8181/v1/policies` returns policy list)
- [ ] OPA picks up `policies/data/users.json` updates without restart (`--watch`)
- [ ] Max body size middleware rejects payloads > 1MB with 413
- [ ] Bootstrap is idempotent: re-running `make up` after first bootstrap does NOT create a second admin (verifies `bootstrap_state` row check)

---

### Phase 2: Governance Engine Core

**Deliverables:**
- `/inspect` endpoint with full Pydantic request/response models + `X-Internal-Token` validation
- `pii.py`: module-level singleton, `asyncio.to_thread()` for both AnalyzerEngine and AnonymizerEngine, asyncio.Lock for init race
- `harm.py`: `harm_scan(text)` plain function using llm-guard PromptInjectionScanner + BanTopicsScanner
- `opa.py`: HTTP client, EVALSHA pattern for Redis (but for OPA direct HTTP, explicit exception handling — NOT bare `except Exception`)
- `pipeline.py`: ordered stage list `[pii_stage, harm_opa_stage]`; `PipelineContext` dataclass
- `context.py`: `PipelineContext` dataclass with all intermediate state
- `audit.py`: `write_audit()` coroutine, metadata-only storage, `written_at` column
- `AuditLog` SQLModel table + Alembic migration (INSERT-only role, monthly partitions, `fillfactor=100`)

**Alembic migration notes (revised after data-integrity review):**

The migration creates `audit_log` + `user_pseudonym_map` + `erasure_log` + `partition_archive_state` + `bootstrap_state` in one transaction. Key revisions from the original sketch: UUIDv7 for `audit_id`, RLS enabled and FORCED, initial monthly partitions pre-created (a partitioned table with no children rejects all INSERTs), and `routing_method` column added.

```python
# governance/alembic/versions/001_initial_schema.py
def upgrade():
    # Pseudonym map — keyed on pseudonym; tenant_id REQUIRED for namespace isolation
    op.create_table(
        "user_pseudonym_map",
        sa.Column("pseudonym",     sa.Text, primary_key=True),
        sa.Column("real_user_id",  sa.Text, nullable=False),
        sa.Column("tenant_id",     sa.Text, nullable=False),
        sa.Column("algorithm",     sa.Text, nullable=False, server_default="hmac-sha256-v1"),
        sa.Column("rotation_id",   sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at",    sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at",    sa.DateTime(timezone=True)),
        sa.UniqueConstraint("real_user_id", "tenant_id", "rotation_id"),
    )

    # Erasure log — auditor-acceptable proof-of-erasure
    op.create_table(
        "erasure_log",
        sa.Column("erasure_id",      postgresql.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id",       sa.Text, nullable=False),
        sa.Column("pseudonym",       sa.Text, nullable=False),
        sa.Column("requested_at",    sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at",    sa.DateTime(timezone=True), nullable=False),
        sa.Column("requestor",       sa.Text, nullable=False),
        sa.Column("audit_row_count", sa.Integer, nullable=False),
    )

    # Partition lifecycle state — makes detach-dump-drop recoverable
    op.create_table(
        "partition_archive_state",
        sa.Column("partition_name", sa.Text, primary_key=True),
        sa.Column("detached_at",    sa.DateTime(timezone=True), nullable=False),
        sa.Column("dumped_at",      sa.DateTime(timezone=True)),
        sa.Column("row_count",      sa.BigInteger),
        sa.Column("s3_key",         sa.Text),
        sa.Column("verified_at",    sa.DateTime(timezone=True)),
        sa.Column("dropped_at",     sa.DateTime(timezone=True)),
    )

    # Bootstrap idempotency — presence of row gates first-startup admin creation
    op.create_table(
        "bootstrap_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("bootstrap_completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bootstrap_token_fingerprint", sa.Text, nullable=False),
        sa.CheckConstraint("id = 1", name="bootstrap_state_singleton"),
    )

    # Audit log — UUIDv7 audit_id (NOT gen_random_uuid), user_id stores PSEUDONYM only
    op.create_table(
        "audit_log",
        sa.Column("audit_id",       postgresql.UUID, primary_key=True),   # UUIDv7 from app layer
        sa.Column("request_id",     postgresql.UUID, nullable=False),
        sa.Column("user_id",        sa.Text, nullable=False),               # PSEUDONYM, never raw
        sa.Column("tenant_id",      sa.Text, nullable=False),
        sa.Column("provider",       sa.Text, nullable=False),               # RESOLVED provider (post-routing)
        sa.Column("model",          sa.Text, nullable=False),
        sa.Column("routing_method", sa.Text, nullable=False),               # explicit_header | table | prefix | tenant_default | catch_all | override_denied
        sa.Column("pii_findings",   postgresql.JSONB),                      # metadata only: [{type, start, end, score}]
        sa.Column("violations",     postgresql.JSONB),
        sa.Column("harm_score",     sa.Numeric(5, 4)),
        sa.Column("decision",       sa.Text, nullable=False),
        sa.Column("latency_ms",     sa.Integer, nullable=False),
        sa.Column("created_at",     sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("written_at",     sa.DateTime(timezone=True), nullable=False),
        postgresql_partition_by="RANGE (created_at)",
    )

    op.execute("""ALTER TABLE audit_log ADD CONSTRAINT decision_check
                  CHECK (decision IN ('allow','block','redact'))""")
    op.execute("""ALTER TABLE audit_log ADD CONSTRAINT routing_method_check
                  CHECK (routing_method IN ('explicit_header','table','prefix','tenant_default','catch_all','override_denied'))""")

    # CRITICAL: pre-create initial partitions or first INSERT fails
    op.execute("""
        CREATE TABLE audit_log_2026_05 PARTITION OF audit_log FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
        CREATE TABLE audit_log_2026_06 PARTITION OF audit_log FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
        CREATE TABLE audit_log_2026_07 PARTITION OF audit_log FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
    """)

    # INSERT-only app role + RLS (defense in depth)
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM gateway_app")
    op.execute("GRANT INSERT, SELECT ON audit_log TO gateway_app")
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY audit_read ON audit_log FOR SELECT USING (
          current_setting('app.current_scope', true) = 'PLATFORM'
          OR (current_setting('app.current_scope', true) = 'TENANT'
              AND tenant_id = current_setting('app.current_tenant_id', true))
          OR user_id = current_setting('app.current_user_id', true)
        )
    """)

    # Append-only trigger (belt-and-suspenders against future role escalation)
    op.execute("""
        CREATE OR REPLACE FUNCTION prevent_audit_mutation() RETURNS TRIGGER AS $$
        BEGIN RAISE EXCEPTION 'audit_log is immutable'; END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER no_audit_mutations
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation();
    """)
```

**SQLAlchemy connection checkout listener** (mandatory — prevents session var leak across pooled connections):
```python
# governance/app/db.py
from sqlalchemy import event

@event.listens_for(engine.sync_engine, "checkout")
def reset_audit_session_vars(dbapi_connection, _conn_record, _conn_proxy):
    with dbapi_connection.cursor() as cur:
        cur.execute("RESET app.current_user_id")
        cur.execute("RESET app.current_tenant_id")
        cur.execute("RESET app.current_scope")
```

**Files to create:**
- `governance/app/db.py` (AsyncEngine + checkout listener)
- `governance/app/pipeline.py`
- `governance/app/context.py`
- `governance/app/pii.py`
- `governance/app/harm.py`
- `governance/app/opa.py`
- `governance/app/audit.py`
- `governance/app/pseudonym.py` (HMAC-SHA256, rotation_id-aware)
- `governance/app/audit_export.py` (keyset-paginated JSONL streamer)
- `governance/app/retention.py` (detach-dump-drop coordinator)
- `governance/alembic/versions/001_initial_schema.py`
- `governance/app/models.py` (InspectRequest, InspectResponse, AuditExportCursor)

**Success criteria:**
- [ ] `POST /inspect` (with valid `X-Internal-Token`) with clean prompt → `{decision: "allow"}`
- [ ] `POST /inspect` (no token) → 403
- [ ] `POST /inspect` with PII → `{decision: "redact", pii_findings: [{type, start, end, score}]}` — no matched text in response
- [ ] `POST /inspect` with jailbreak attempt → `{decision: "block"}`
- [ ] OPA deny rule fires + allow=true → still blocks (deny+allow check)
- [ ] `audit_log.user_id` contains pseudonym, NOT raw user id (verified by SQL query in test)
- [ ] `audit_log.audit_id` is UUIDv7 (test asserts version nibble == 7)
- [ ] Initial 3 monthly partitions exist; INSERT succeeds for current-month timestamp
- [ ] RLS denies read when no session vars set (empty `current_setting` returns NULL → no condition matches)
- [ ] RLS allows read when `app.current_scope='SELF'` AND `user_id` matches
- [ ] Connection checkout listener observed: session vars from previous request do not leak into next request (concurrent test with 2 different scopes)
- [ ] Audit record written with `written_at` column populated
- [ ] `routing_method` column populated on every audit row
- [ ] `DELETE /v1/users/{id}` (admin only) sets `deleted_at` on `user_pseudonym_map`, overwrites `real_user_id` with `'[ERASED]'`, inserts `erasure_log` row with `audit_row_count`
- [ ] After erasure, the pseudonym still appears in `audit_log` but lookups via `user_pseudonym_map` return `'[ERASED]'`
- [ ] Presidio initialized once (lock prevents re-init on concurrent startup)
- [ ] `GET /v1/audit/export?format=jsonl` streams NDJSON with `Link: rel="next"` containing `(created_at, audit_id)` cursor
- [ ] `pytest governance/` passes, including asyncio tests with `asyncio_mode=auto`

---

### Phase 3: Reference Proxy

**Deliverables:**
- OpenAI-compatible routes: `POST /v1/chat/completions`, `GET /v1/models`, `GET /v1/me`
- Admin routes: `POST /v1/keys`, `DELETE /v1/users/{id}` (GDPR erasure), `GET /v1/audit`, `GET /v1/audit/export`
- `bootstrap.py`: first-startup admin key creation gated by `bootstrap_state` row; `os.environ.pop("GATEWAY_BOOTSTRAP_TOKEN")` after use
- `auth.py`: bcrypt hash verification + TTLCache (60s, maxsize=1000) for API key lookup; JWT with `algorithms=["HS256"]` pinned
- `rate_limit.py`: Lua script returns `{allowed, retry_after_seconds}`; ZREMRANGEBYSCORE + INCR counter + unconditional EXPIRE; EVALSHA at startup; keyed on `user_id` from CallerContext
- `routing.py`: resolves provider BEFORE `/inspect` call via override-header (permissioned) → `models.yaml` → prefix → tenant default → 400. The resolved provider is what goes into the `InspectRequest`
- `governance_client.py`: sends `X-Internal-Token` and the RESOLVED provider; tenacity retry (3 attempts); fail-closed block on all failures
- `headers.py`: builders for OpenAI-shape error envelopes with `violations[]`, `x-ratelimit-*` headers on every response, `Retry-After` + `retry-after-ms` on 429, `X-Gateway-Pii-Redacted` + `X-Gateway-Pii-Types` when redaction occurs and tenant policy ≠ `silent`
- Provider dispatch via `match/case` (no ABC or registry, no LiteLLM)
- `MockAdapter`: pattern-matches on input → canned scenario; async SSE generator for streaming; activated by `settings.mock_mode`
- `OpenAIAdapter`: reference, httpx AsyncClient with `limits=httpx.Limits(max_connections=50, max_keepalive_connections=20)`
- Streaming: tiered inspection; `strict_response_inspection` config option for buffered mode
- CORS middleware with explicit `allow_origins` list
- FastAPI app with `docs_url=None, redoc_url=None` in production; enabled in dev via env flag
- Max body size middleware (1MB hard limit)

**Files to create:**
- `proxy/app/main.py` (routes, lifespan, CORS, body size limit, docs URL toggle)
- `proxy/app/config.py` (settings, mock_mode resolution at startup)
- `proxy/app/bootstrap.py`
- `proxy/app/auth.py` (bcrypt + TTLCache + JWT HS256 pinned)
- `proxy/app/rate_limit.py` (Lua + EVALSHA + retry_after return)
- `proxy/app/routing.py` (5-step provider resolution)
- `proxy/app/governance_client.py`
- `proxy/app/headers.py`
- `proxy/app/providers/openai.py`
- `proxy/app/providers/mock.py`
- `proxy/app/models.py`
- `tests/fixtures/mock_scenarios.py`

**Success criteria:**
- [ ] `curl -H "X-API-Key: test-key" localhost:8741/v1/chat/completions -d '...'` → forwards to resolved provider
- [ ] API key validated via bcrypt (not plaintext comparison)
- [ ] Invalid API key → 401; expired JWT → 401
- [ ] **Routing**: `model=claude-3-5-sonnet-20241022` with no header → Anthropic (prefix inference); `X-Gateway-Provider: anthropic` from caller WITH `gateway:provider_override:anthropic` → Anthropic (explicit); same header from caller WITHOUT permission → routes normally AND emits audit row with `routing_method=override_denied`
- [ ] **OPA-after-resolution invariant**: send `model=gpt-4o` + `X-Gateway-Provider: anthropic` (permissioned) + PHI content where anthropic is not in approved providers → blocked. Send same without override → allowed iff openai is approved. This proves OPA evaluates resolved provider.
- [ ] `GET /v1/models` returns OPA-filtered set in OpenAI Model shape (`{id, object, created, owned_by}`)
- [ ] `GET /v1/me` returns user/tenant/roles/allowed_models/rate_limit/pii_policy
- [ ] Rate limit: exactly N requests succeed at limit=N, N+1 gets 429 (no over-admit)
- [ ] Every response (200 + 429) carries `x-ratelimit-limit-requests`, `x-ratelimit-remaining-requests`, `x-ratelimit-reset-requests`
- [ ] 429 carries `Retry-After` (int seconds) AND `retry-after-ms`
- [ ] PII redaction emits `X-Gateway-Pii-Redacted: true` + `X-Gateway-Pii-Types` (types only); silent tenant emits neither
- [ ] Error envelope shape: OpenAI SDK parses `error.type`/`code` and surfaces `violations[]` without crashing
- [ ] `required_roles` present in error body; `current_roles` is NOT (asserted)
- [ ] `MOCK_PROVIDERS=true`: `make demo` runs all 6 scenarios with no network calls; CI completes without API keys
- [ ] `OPENAI_API_KEY=mock`: same effect via sentinel-key path
- [ ] Governance engine unavailable → 503 (fail-closed, no forward to provider)
- [ ] `/docs` returns 404 when `DOCS_ENABLED=false`
- [ ] Request > 1MB → 413
- [ ] CORS: request from unlisted origin → blocked
- [ ] `pytest proxy/` passes

---

### Phase 4: Provider Adapters

**Deliverables:**
- `AnthropicAdapter`: translates OpenAI messages → Anthropic Messages API; streaming via `aiter_bytes()`
- `GeminiAdapter`: translates to Gemini REST API
- `OllamaAdapter`: OpenAI-compatible, configurable base URL
- `GenericAdapter`: OpenAI-compatible pass-through
- `extract_usage(response) -> UsageMetrics`: shared across adapters for future token tracking
- Provider dispatch in `proxy/app/main.py`:

```python
match provider:
    case "openai":   adapter = OpenAIAdapter(client, config)
    case "anthropic": adapter = AnthropicAdapter(client, config)
    case "gemini":    adapter = GeminiAdapter(client, config)
    case "ollama":    adapter = OllamaAdapter(client, config)
    case _:           adapter = GenericAdapter(client, config)
```

**Files to create:**
- `proxy/app/providers/anthropic.py`
- `proxy/app/providers/gemini.py`
- `proxy/app/providers/ollama.py`
- `proxy/app/providers/generic.py`

**Success criteria:**
- [ ] Anthropic adapter: request in OpenAI format → Anthropic API called → response in OpenAI format
- [ ] Ollama adapter: request → local Ollama → response
- [ ] Unknown provider string → 400 (match default case)
- [ ] Each adapter handles `stream: true`
- [ ] `pytest proxy/tests/test_adapters.py` passes

---

### Phase 5: OPA Policies and Testing

**Deliverables:**
- Full Rego policy in `policies/llm/authz.rego` (model access + data classification, runs against RESOLVED provider)
- `policies/llm/audit_scope.rego` mapping caller roles → `SELF | TENANT | PLATFORM` scope enum used by `/v1/audit`
- `policies/llm/provider_override.rego` checking `gateway:provider_override:<provider>` permission per-provider
- `policies/llm/allow_model.rego` for `/v1/me` and `/v1/models` enumerate-and-filter (single-model allow query, not partial evaluation)
- `authz_test.rego` with all required test cases (including deny+allow coexistence, provider-override scoping, audit-scope mapping)
- `make opa-test` runs `opa test policies/`

**Critical test case (security-sentinel):**
```rego
# policies/llm/authz_test.rego
test_phi_to_non_approved_provider_blocks_even_when_allow_fires {
    # tier2 user sending PHI to non-HIPAA provider
    # allow = true (user has tier2 access, model is tier2)
    # deny = non-empty (PHI to non-approved provider)
    # Pipeline must check BOTH — block is the correct outcome
    result := data.llm.authz with input as {
        "user": {"id": "u1", "roles": ["tier2-access"]},
        "request": {
            "model": "gpt-4o",
            "provider": "openai",
            "data_classification": ["PHI"],
        }
    }
    result.allow == true          # allow fires (user has access to tier2)
    count(result.deny) > 0        # deny also fires (PHI to non-approved)
    # The PIPELINE (not OPA) must treat this as a block
}
```

**Success criteria:**
- [ ] `make opa-test` passes (all rego tests green)
- [ ] PHI + non-approved-provider + tier2 user → `allow=true` AND `deny` non-empty
- [ ] Pipeline blocks when `allow=true` but `deny` non-empty
- [ ] tier2 model + non-tier2 role → deny
- [ ] `provider_override.rego`: caller with `gateway:provider_override:anthropic` but NOT `gateway:provider_override:gemini` → header `X-Gateway-Provider: gemini` is ignored
- [ ] `audit_scope.rego`: platform_admin → `PLATFORM`; tenant_admin → `TENANT`; default → `SELF`; unknown role → `SELF` (default-deny)
- [ ] OPA unreachable → fail-closed block

---

### Phase 6: Deployment and Portfolio Polish

**Deliverables:**
- Fly.io config: OPA as separate Fly app (`opa.internal:8181`), governance as `performance-1x` (1GB RAM)
- `auto_stop_machines = true` on governance and proxy Fly apps
- `SPACY_MODEL` env var: `en_core_web_lg` locally, `en_core_web_sm` on Fly.io
- `fly secrets set` for: `JWT_SECRET`, `GOVERNANCE_INTERNAL_TOKEN`, `GATEWAY_BOOTSTRAP_TOKEN`, `PSEUDONYM_HMAC_KEY`, all provider API keys, `DATABASE_URL` (internal Fly Postgres URL)
- Non-secret config in `fly.toml` `[env]`: `OPA_URL=http://opa.internal:8181`, `LOG_LEVEL=info`, `DOCS_ENABLED=false`
- Fly.io **scheduled job** (cron-style machine) for `scripts/rotate_partitions.py`: nightly at 02:00 UTC; verifies `pg_partman` is NOT in use (we use native partitioning), creates next month's partition, walks `partition_archive_state` to advance any stuck rows, detaches partitions older than `RETENTION_HOT_MONTHS=12`, dumps detached partitions to S3, drops verified partitions
- Drift-detection `/health` sub-check: returns "degraded" if any partition has `detached_at > 48h ago AND dumped_at IS NULL`
- `README.md`: architecture diagram (Mermaid C4, per CLAUDE.md), quickstart, demo scenarios, design decisions, portfolio notes including the explicit "no LiteLLM" rationale
- `docs/demo-scenarios.md`: 6 canned scenarios (matches mock fixture set)
- `make demo` script runs all 6 scenarios with expected outcomes — works with `MOCK_PROVIDERS=true` so portfolio reviewers need no API keys

**Fly.io deployment notes (system-architect):**
- Each service is a separate `fly.toml` file (proxy, governance, opa)
- Only proxy (8741) gets a `[[services]]` public endpoint
- Governance (8742) and OPA (8181) must have NO `[[services]]` — internal network only
- Governance engine needs `performance-1x` (1GB RAM) — `en_core_web_lg` alone is ~680MB
- OPA is lightweight: `shared-cpu-1x` (256MB) is sufficient
- Presidio warm-up: governance `/health` returns 503 until ready; `/health` endpoint should be retried by demo script before showing the demo

**`en_core_web_sm` on Fly.io:**
- `en_core_web_sm` = ~13MB vs `en_core_web_lg` = ~830MB
- Use `SPACY_MODEL` env var; Dockerfile downloads at build time: `RUN uv run python -m spacy download ${SPACY_MODEL:-en_core_web_lg}`
- Document: `sm` detects same entity types with lower recall — acceptable for demo, not for production

**Files to create:**
- `README.md`
- `fly.toml`, `fly-governance.toml`, `fly-opa.toml`
- `.envrc.example`
- `docs/demo-scenarios.md`
- `scripts/demo.sh`

**Success criteria:**
- [ ] `make up && make demo` runs all 5 scenarios, prints pass/fail
- [ ] Governance service starts in <30s on cold Fly.io start (with `en_core_web_sm`)
- [ ] Fly.io demo URL reachable
- [ ] `README.md` self-sufficient for a reviewer unfamiliar with the project
- [ ] No secrets in git (pre-commit hook active)
- [ ] `/docs` disabled on Fly.io deployments

---

## System-Wide Impact

### Interaction Graph

Request flow (two levels deep):
1. `POST /v1/chat/completions` → CORS check → body size limit (1MB) → `AuthMiddleware.validate()` → TTLCache hit or bcrypt verify → resolves to `CallerContext`
2. `CallerContext` → `check_rate_limit()` → EVALSHA (Lua: ZREMRANGEBYSCORE + ZCOUNT + ZADD with INCR) on `user_id` key and `api_key_prefix` key
3. Rate limit passed → `GovernanceClient.inspect()` with `X-Internal-Token` → HTTP POST `governance:8742/inspect`
4. `/inspect` → validates `X-Internal-Token` → `pipeline.run(ctx)`:
   - Stage 1: `pii_stage(ctx)` → `asyncio.to_thread(analyzer.analyze)` → sets `ctx.pii_findings` + `ctx.data_classification`
   - Stage 2: `harm_opa_stage(ctx)` → `asyncio.gather(harm.scan(ctx.text), opa.evaluate(ctx.to_opa_input()))` — both use `data_classification` from stage 1
5. Decision check: block if `decision=="block"` OR `not opa.allow` OR `len(opa.deny)>0`
6. Response returned to proxy + `BackgroundTasks(write_audit)` queued
7. Proxy: block → 403; redact → forward `ctx.redacted_text` to provider; allow → forward original
8. Provider response → `StreamingResponse(aiter_bytes())` to client

### Error Propagation

| Error | Where it's handled | Client sees |
|---|---|---|
| Invalid API key | AuthMiddleware (bcrypt fail) | 401 |
| JWT expired / wrong alg | AuthMiddleware | 401 |
| Request > 1MB | Body size middleware | 413 |
| Rate limit exceeded | RateLimiter (EVALSHA) | 429 |
| Governance engine 5xx | GovernanceClient (tenacity 3 retries) | 503 |
| OPA timeout (50ms) | OPAClient (explicit TimeoutException) | 403 (fail-closed block) |
| OPA missing result | OPAClient (missing key check) | 403 (fail-closed block) |
| Presidio timeout | PIIDetector (asyncio timeout) | 403 (fail-closed block) |
| Provider 5xx | ProviderAdapter (proxied) | 502 |
| Audit write failure | AuditWriter (logs to stderr, proceeds) | Transparent |
| Missing X-Internal-Token on /inspect | Governance middleware | 403 |

### State Lifecycle Risks

- **Rate limit race**: Mitigated by atomic Lua script (ZREMRANGEBYSCORE + ZCOUNT + ZADD in single EVALSHA). NOT mitigated for distributed Redis cluster — document this limitation.
- **Presidio startup delay**: Mitigated by `asyncio.Lock()` (prevents concurrent init), `start_period: 30s` in Docker Compose, and `/health` returning 503 until ready.
- **OPA bundle reload**: `--watch` flag in dev causes ~50-200ms policy evaluation gap during reload. Document as known behavior. Remove `--watch` in production.
- **Partial stream + violation**: Client receives partial content before chunk-level error event. Log partial content and `audit_id`. Document as insufficient for HIPAA response-side compliance.
- **Audit write loss on crash**: `BackgroundTasks` has no durability — loss is silent on process crash. Documented limitation; migration path to Redis Streams / transactional outbox in v2.
- **`audit_id` eventually consistent**: Returned to client before write completes. Document in API contract; `/audit/{audit_id}` may return 404 briefly.

### API Surface Parity

The governance engine `/inspect` API is the only inter-service contract. Any proxy implementation (Envoy ext_authz, nginx, Rust) must:
1. Call `POST /inspect` with `X-Internal-Token` header
2. Send `CallerContext` in the `caller` field (flat, with `user_id`, `tenant_id`, `roles`)
3. Check: block if `decision=="block"` OR (inspect response contains `deny` set, even if `allow==true`)
4. Document this contract in the OpenAPI spec and README

The `phase` discriminator field in the request prepares for future response-side inspection without a breaking change.

### Integration Test Scenarios

1. **PII redaction end-to-end**: Send prompt with SSN → verify redacted content reaches OpenAI mock, `pii_findings` in audit has `{type, start, end}` but NO matched text
2. **OPA deny + allow coexistence**: tier2 user + tier2 model + PHI + non-approved provider → `allow=true`, `deny` non-empty → proxy returns 403
3. **Rate limit atomic boundary**: Fire 20 concurrent requests at limit=10 → verify exactly 10 succeed, 10 get 429 (no over-admit from INCR counter fix)
4. **Governance engine unavailable**: Stop governance container → proxy returns 503, zero requests forwarded to provider
5. **X-Internal-Token missing**: Call `/inspect` directly without token → 403 (governance engine self-protection)

## Acceptance Criteria

### Functional Requirements

- [ ] OpenAI-compatible `POST /v1/chat/completions` works with `openai` Python SDK against the proxy
- [ ] PII detected and redacted; audit log stores metadata only (type + offset), never matched text
- [ ] OPA blocks when `deny` fires, even if `allow=true` simultaneously
- [ ] Jailbreak attempts detected and blocked
- [ ] Every request in audit log; `written_at` column populated; `routing_method` populated
- [ ] `audit_log.user_id` is HMAC pseudonym, never raw identifier
- [ ] `GET /v1/audit` filterable via FastAPI `/docs`; user-scoped by default, OPA-derived `SELF|TENANT|PLATFORM`
- [ ] `GET /v1/audit/export?format=jsonl` streams keyset-paginated NDJSON with `Link: rel="next"`
- [ ] `GET /v1/me` returns caller's effective state (roles, allowed_models, rate limit, pii policy)
- [ ] `DELETE /v1/users/{id}` (admin) performs GDPR erasure: `deleted_at` + `'[ERASED]'` overwrite + `erasure_log` entry
- [ ] All 5 provider adapters (`openai`, `anthropic`, `gemini`, `ollama`, `generic`) + `mock` work via `match/case` dispatch — no LiteLLM dependency
- [ ] Provider resolution happens BEFORE `/inspect` (verified by integration test using `X-Gateway-Provider` to flip the routed provider mid-test)
- [ ] `MOCK_PROVIDERS=true` enables full demo with no upstream API keys
- [ ] Streaming responses pass through (SSE chunks delivered)
- [ ] Rate limiting: atomic, keyed on `user_id` (not headers), no over-admit; rate-limit headers on every response

### Security Requirements

- [ ] API keys stored as `(prefix, bcrypt_hash)` — never plaintext
- [ ] JWT validated with `algorithms=["HS256"]` pinned — `none` algorithm rejected
- [ ] `/inspect` requires `X-Internal-Token` validated with `secrets.compare_digest`
- [ ] CORS: explicit `allow_origins` whitelist on both services
- [ ] `/docs` disabled in production (`DOCS_ENABLED` env flag)
- [ ] Request body capped at 1MB on both services
- [ ] Audit log INSERT-only at Postgres privilege level + trigger rejects mutations + RLS enforced with FORCE
- [ ] `gateway_app` Postgres role is not table owner and not superuser (verified by migration assertion)
- [ ] Bootstrap admin key creation is gated by `bootstrap_state` row (not "no admin exists") — deletion of admin keys does NOT re-trigger bootstrap
- [ ] `GATEWAY_BOOTSTRAP_TOKEN` is popped from `os.environ` after consumption
- [ ] OPA evaluates the RESOLVED provider, not the inferred provider (asserted in integration test)
- [ ] `X-Gateway-Provider` permission is scoped per-provider (`gateway:provider_override:<name>`)
- [ ] Audit-log `routing_method=override_denied` is written when header is present without matching permission
- [ ] Connection-pool checkout listener resets `app.current_*` session vars on every connection acquisition
- [ ] Scope value from OPA is allowlist-validated before any `SET LOCAL` invocation
- [ ] `error.required_roles` is present in governance violations; `current_roles` is never echoed
- [ ] `entity_types_detected` carries types only — never offsets, matched substrings, or scores
- [ ] No transitive dependency on LiteLLM or other LLM-gateway libraries (verified by `uv tree`)

### Non-Functional Requirements

- [ ] Pre-call governance overhead < 20ms P99 with `en_core_web_sm`; documented miss on prompts >1000 tokens with `en_core_web_lg`
- [ ] Presidio initialized once at startup (asyncio.Lock prevents re-init race)
- [ ] Audit writes never block the request path
- [ ] Governance engine unavailability blocks requests — no silent pass-through
- [ ] `uv` for all Python deps; no `pip install` anywhere

### Quality Gates

- [ ] `make test` passes for both services (`pytest --asyncio-mode=auto`)
- [ ] `make lint` passes (ruff + pyright)
- [ ] `make opa-test` passes including deny+allow coexistence test
- [ ] `make up` succeeds from clean state in < 90 seconds (Presidio warmup included)
- [ ] README self-sufficient for reviewer to run demo

## Alternative Approaches Considered

See brainstorm: `docs/brainstorms/2026-05-26-llm-governance-gateway-brainstorm.md`

- **Monolithic FastAPI**: Rejected — proxy coupled to Python
- **Plugin pipeline monolith**: Rejected — same coupling
- **OPA for everything**: Rejected — Rego awkward for threshold/routing config
- **TypeScript/Hono**: Rejected — weak PII/NLP ecosystem
- **HarmClassifier ABC**: Rejected — YAGNI (code-simplicity-reviewer)
- **asyncio.Queue audit mode**: Rejected — YAGNI for portfolio (code-simplicity-reviewer)
- **`flag` action for PII**: Rejected — redact-or-block is complete (code-simplicity-reviewer)

## Dependencies and Prerequisites

- Docker Desktop (for `make up`)
- OpenAI API key (for live testing)
- Anthropic, Gemini API keys (optional)
- Ollama running locally (optional)
- Fly.io account with credit card (free tier insufficient for `en_core_web_lg` memory footprint)

## Risk Analysis

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Presidio spaCy model too slow | Medium | High | `en_core_web_sm` on Fly; `en_core_web_lg` locally |
| Fly.io memory OOM on governance | High | High | `performance-1x` (1GB) + `auto_stop_machines` |
| OPA policy complexity grows | Low | Medium | `opa test` enforces correctness; keep Rego simple |
| Streaming governance latency | Medium | Medium | Chunk regex <1ms; pre-call is the bottleneck |
| Portfolio reviewer can't run demo | Low | High | Fly.io URL + `make demo` script with pre-warm |
| Auth TTLCache stale key | Low | Medium | 60s TTL; key revocation takes up to 60s to propagate |
| Audit write loss on crash | Medium | Medium | Document limitation; v2 migration to Redis Streams |
| Partition retention drift (cron skip) | Medium | Medium | `partition_archive_state` reconciliation + `/health` "degraded" when stuck > 48h |
| Detach-dump-drop interrupted halfway | Low | High | State table tracks each step; recovery script resumes at first NULL column; never drops without `verified_at` |
| Bootstrap token theft from process env | Low | Critical | `os.environ.pop()` after consumption; Fly secrets isolation; documented rotation procedure |
| Provider-override exfiltration | Low | High | OPA evaluates resolved provider; per-provider permission scope; `override_denied` audit row |
| Session-var leak between pooled connections | Medium | High | Mandatory checkout listener RESETs `app.current_*`; allowlist validation before any `SET LOCAL` |
| Pseudonym correlation re-identification | Low | Medium | `rotation_id`-based key rotation; documented residual; HMAC key in Fly secrets |
| Partitioned table inserts fail (no children) | High pre-fix | Critical | Pre-create 3 monthly partitions in initial migration; CI test asserts first INSERT succeeds |

## Future Considerations (Out of Scope for v1)

- Provider failover / retry-with-fallback (own implementation; **not LiteLLM router** per Design Principles)
- Response-side full content inspection (buffered; `strict_response_inspection=true` config option prepared)
- Transactional outbox / Redis Streams for durable audit writes
- Token/cost tracking and chargeback (`extract_usage()` method on adapters prepared; `x-ratelimit-*-tokens` header namespace reserved)
- mTLS for inter-service auth (upgrade from `X-Internal-Token`)
- Admin dashboard UI
- JWT/OIDC integration with external identity provider
- Kubernetes Helm chart with Envoy ext_authz (gRPC)
- ML-based harm classifier (module swap via `harm.py`)
- Pseudonym `rotation_id` automated bump on a documented schedule (correlation defense)
- Audit export in CSV format (deferred — requires JSONB column-promotion product decision)
- S3 cold-storage retrieval API for archived partitions (operator runbook only in v1)
- Tenant self-service for OPA policy authoring (sandboxed Rego upload)

## Sources & References

### Origin

- **Brainstorm:** [docs/brainstorms/2026-05-26-llm-governance-gateway-brainstorm.md](../brainstorms/2026-05-26-llm-governance-gateway-brainstorm.md)
  Key decisions carried forward: sidecar governance service (ext_authz), OPA+YAML hybrid policy, Presidio PII, rules-only harm (pluggable module), API keys + JWT auth

### Research Agents

- Security: security-sentinel (12 findings, 4 critical, + 3 more from gap review: bootstrap re-trigger, provider-override ordering, session-var leak)
- Architecture: architecture-strategist + backend-architect (pipeline ordering fix, CallerContext shape, GovernancePipeline as middleware chain)
- Python quality: kieran-python-reviewer (asyncio.to_thread, Lua INCR, bare except)
- Performance: performance-oracle (latency budget, auth cache, en_core_web_sm)
- Data integrity: data-integrity-guardian (metadata-only audit, written_at, INSERT-only role, + UUIDv7, tenant_id on pseudonym map, initial partitions, keyset pagination, HMAC pseudonyms)
- Simplicity: code-simplicity-reviewer (~40% abstraction reduction)
- Deployment: system-architect (Fly.io memory, Docker Compose healthcheck chain)

### Pre-Build Gap Resolution (2026-05-26)

Source: `docs/gaps/2026-05-26-pre-build-gaps.md` — six gaps, each researched via `compound-engineering:research:best-practices-researcher` (5 parallel passes covering IaC provisioning, provider routing, error semantics, mock provider, audit retention) and then adversarially reviewed by `compound-engineering:review:security-sentinel` (bootstrap key, header override, RLS) and `compound-engineering:review:data-integrity-guardian` (retention, GDPR erasure, export). The review pass produced 6 material revisions to the initial recommendations; all are captured in the "Critical Corrections" list and the "Pre-Build Gap Resolutions" section.

Notable position taken: **no LiteLLM dependency**. The gap research surfaced LiteLLM's `config.yaml` pattern as a reference but the project's stated security posture (minimize transitive dependencies; copy small focused code rather than absorb feature-rich libraries) rules out direct adoption. Provider routing, model configuration, and adapter implementations are owned in-tree.

### Learned Skills Applied

- `cors-security-explicit-allowlists` — explicit CORS allowlist on both services
- `race-condition-prevention-select-for-update` — asyncio.Lock on Presidio init
- `alembic-migration-with-data-cleanup` — audit log migration with constraints and triggers

### External References

- [Microsoft Presidio Documentation](https://microsoft.github.io/presidio/)
- [OPA REST API Reference](https://www.openpolicyagent.org/docs/rest-api)
- [llm-guard library](https://github.com/protectai/llm-guard)
- [LiteLLM AI Gateway patterns](https://docs.litellm.ai/docs/simple_proxy)
- [OWASP LLM Top 10 2025 — LLM01 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
- [fastapi-opa library](https://github.com/busykoala/fastapi-opa)
- [Fly.io internal networking](https://fly.io/docs/reference/private-networking/)
