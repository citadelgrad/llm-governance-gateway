# AI Gateway

Production-grade LLM proxy with governance, PII redaction, and policy enforcement.

---

## Quickstart

**Prerequisites**

- Docker + Docker Compose v2
- [direnv](https://direnv.net/) (or set env vars manually)
- Python 3.11+ with [uv](https://github.com/astral-sh/uv)

**1. Clone**

```bash
git clone <repo-url> ai-gateway
cd ai-gateway
```

**2. Configure environment**

```bash
cp .envrc.example .envrc   # if provided, else create manually
direnv allow
```

Minimum required variables:

```bash
export JWT_SECRET="change-me-in-production"
export GOVERNANCE_INTERNAL_TOKEN="change-me-in-production"
export DATABASE_URL="postgresql://gateway:gateway@localhost:5432/gateway"
export PSEUDONYM_HMAC_KEY="change-me-in-production"
```

**3. Start services**

```bash
make up
```

Expected output:

```
[+] Running 6/6
 ✔ Container ai-gateway-postgres-1    Healthy
 ✔ Container ai-gateway-redis-1       Healthy
 ✔ Container ai-gateway-opa-1         Healthy
 ✔ Container ai-gateway-migrate-1     Exited (0)
 ✔ Container ai-gateway-governance-1  Healthy
 ✔ Container ai-gateway-proxy-1       Healthy
```

**4. Provision and run demo**

```bash
make demo
```

This runs `make up`, waits for health checks, then executes the idempotent provisioner (`scripts/provision.py`) which seeds tenants, users, and model config into Postgres and writes OPA data documents.

**5. Send a test request**

```bash
# Health check
curl http://localhost:8765/health
# {"status": "ok"}

# Chat completions (requires provisioned API key)
curl -X POST http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer gw_<your-key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hello"}]}'
```

**Other useful targets**

| Target | Description |
|---|---|
| `make status` | Show container health |
| `make logs` | Follow all service logs |
| `make test` | Unit tests (proxy + governance) |
| `make test-integration` | Integration smoke tests (requires `make up`) |
| `make opa-test` | OPA Rego policy unit tests |
| `make lint` | ruff + pyright on both services |
| `make down` | Stop all services |

---

## Architecture

### C4 Context Diagram

```mermaid
flowchart TB
    user["Client / API Consumer"]:::person
    gateway["AI Gateway"]:::system
    openai["OpenAI"]:::external
    anthropic["Anthropic"]:::external
    gemini["Google Gemini"]:::external
    ollama["Ollama (local)"]:::external

    user -->|"JWT Bearer + chat request"| gateway
    gateway -->|"forwarded prompt"| openai
    gateway -->|"forwarded prompt"| anthropic
    gateway -->|"forwarded prompt"| gemini
    gateway -->|"forwarded prompt"| ollama

    classDef person fill:#1168bd,color:#fff,stroke:#0b4884
    classDef system fill:#2d7d46,color:#fff,stroke:#1a5c33
    classDef external fill:#666,color:#fff,stroke:#444
```

**Legend**

| Node | Type | Notes |
|---|---|---|
| Client / API Consumer | Person | Any HTTP client using OpenAI-compatible API |
| AI Gateway | System | This project — proxy + governance + OPA + Postgres |
| OpenAI / Anthropic / Gemini / Ollama | External System | Upstream LLM providers |

---

### C4 Container Diagram

```mermaid
flowchart TB
    subgraph gateway_system["AI Gateway System"]
        proxy["Proxy\n(FastAPI)\n:8765 public"]:::container
        governance["Governance\n(FastAPI + spaCy + Presidio)\ninternal only"]:::container
        opa["OPA\n(Rego policies)\n:8181 internal"]:::container
        postgres["PostgreSQL\n(audit log + pseudonyms)\ninternal only"]:::container
        redis["Redis\n(rate limiting)\ninternal only"]:::container
    end

    client["Client"]:::person
    llm["LLM Providers"]:::external

    client -->|"HTTPS + JWT"| proxy
    proxy -->|"POST /inspect"| governance
    governance -->|"POST /v1/data/llm/authz"| opa
    governance -->|"audit write (async)"| postgres
    governance -->|"pseudonym lookup"| postgres
    proxy -->|"rate limit check"| redis
    proxy -->|"forward request"| llm

    classDef person fill:#1168bd,color:#fff,stroke:#0b4884
    classDef container fill:#2d7d46,color:#fff,stroke:#1a5c33
    classDef external fill:#666,color:#fff,stroke:#444
```

**Legend**

| Container | Tech | Responsibility |
|---|---|---|
| Proxy | FastAPI, asyncpg, httpx | Auth, routing, rate limiting, PII header propagation |
| Governance | FastAPI, spaCy, Presidio | PII detection + pseudonymization, harm scoring, policy pipeline |
| OPA | Open Policy Agent | Rego-based authz: model tiers, PHI routing, prompt injection |
| PostgreSQL | Postgres 16 | Append-only partitioned audit log, pseudonym map, erasure log |
| Redis | Redis 7 | Sliding-window rate limiter (Lua script, per-user) |

---

### Request Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant P as Proxy :8765
    participant RL as Redis (rate limiter)
    participant G as Governance
    participant OPA as OPA
    participant DB as PostgreSQL
    participant LLM as LLM Provider

    C->>P: POST /v1/chat/completions (JWT)
    P->>P: authenticate JWT / API key
    P->>RL: sliding window check (user_id)
    RL-->>P: allowed / rate_limited
    P->>G: POST /inspect (text, tenant_id, user_id, model_id)
    G->>G: spaCy + Presidio PII scan
    G->>G: harm scoring
    G->>OPA: POST /v1/data/llm/authz (phase, user, request)
    OPA-->>G: {allow, deny_reasons, redact_pii}
    G->>DB: write audit_log row (async background task)
    G-->>P: {decision, redacted_text, pii_findings, audit_id}
    alt decision == "block"
        P-->>C: 403 policy_violation
    else decision == "allow"
        P->>LLM: forwarded request (PII replaced with pseudonyms)
        LLM-->>P: completion response
        P-->>C: response + X-Audit-Id + X-PII-Redacted headers
    end
```

---

## Design Decisions

### Why Not LiteLLM?

The proxy ships custom provider adapters (`proxy/app/providers/`) for OpenAI, Anthropic, Gemini, Ollama, and a generic OpenAI-compatible backend. This was a deliberate choice rather than wrapping LiteLLM.

**Reasons:**

- **Control over request/response transformation** — PII pseudonymization requires rewriting message content in-flight; generic wrappers make this fragile.
- **Dependency risk** — LiteLLM is a large, rapidly-changing dependency. A custom adapter is ~50 LOC per provider and has no transitive surprises.
- **Engineering depth** — Building adapters demonstrates understanding of provider API differences (Anthropic's `x-api-key` vs OpenAI Bearer, Gemini's distinct schema) rather than delegating to an abstraction layer.
- **Streaming** — Each provider's streaming format differs; custom adapters handle this transparently without fighting a framework.

### Fail-Closed Boundaries

If the governance service or OPA is unreachable, the proxy returns `503 governance_unavailable` — the request is **denied**, not passed through. This is enforced in `proxy/app/governance_client.py` via `GovernanceError` propagation. The same pattern applies at the OPA layer inside governance: a connection failure to OPA results in `block`, not `allow`.

**Rationale:** In a regulated environment, a silent passthrough on control-plane failure is a compliance incident. Fail-closed trades availability for auditability.

### Audit Log as Metadata

Every request through governance generates a UUID7 `audit_id` returned in the `InspectResponse`. The proxy surface area propagates this value to the caller via response headers (`X-Audit-Id`). The audit record includes:

- `user_id` (pseudonymized), `tenant_id`, `model_id`, `routing_method`
- `decision` (allow/block), `violations`, `harm_score`
- `pii_findings` (types only — not the raw PII values)
- `created_at` (event time) and `written_at` (DB write time)

The audit table is append-only and partitioned. Audit rows are written asynchronously via FastAPI `BackgroundTasks` to keep the hot path latency low.

### GDPR Pseudonymization

PII entities detected by Presidio are replaced with HMAC-SHA256 keyed pseudonyms before the text leaves the governance service. Key properties:

- **Deterministic** — the same (PII value, key) pair always produces the same pseudonym, enabling consistent audit correlation without storing the original.
- **Keyed** — pseudonyms are only reversible with the HMAC key (`PSEUDONYM_HMAC_KEY`), which is never logged.
- **Rotation** — pseudonym partitions can be rotated (`make rotate-partitions`). After rotation, new requests get pseudonyms under the new key. Old partitions are archived; the mapping is severed.
- **Right to erasure** — `DELETE /v1/users/{user_id}` overwrites `real_user_id` with `[ERASED]` in `user_pseudonym_map` and writes an `erasure_log` entry. The audit rows remain (for compliance) but the link from pseudonym to real user is destroyed.

### PostgreSQL RLS + FORCE

All audit and pseudonym tables have `ROW SECURITY FORCE` enabled. Policies restrict reads to the owning tenant. `FORCE` means the policy applies even to the table owner and superuser roles — a misconfigured application connection cannot accidentally read another tenant's audit rows.

### OPA Deny+Allow Pattern

All Rego policies follow the deny+allow structure: the default is `allow := false`. Explicit `allow` rules must fire for a request to proceed. Deny reasons are collected as a set and surfaced in the `violations` list returned to the proxy.

Model tiers are enforced at the policy layer: tier-1 models are open to all authenticated users; tier-2 models require the `tier2-access` role. PHI is blocked from routing to non-HIPAA-BAA providers — only `azure-openai` and `bedrock` are in the approved set. Policy files live in `policies/llm/` and are hot-reloaded by OPA via `--watch`.

---

## Deployment (Fly.io)

The project targets a three-app topology on Fly.io:

| App | Exposure | Services |
|---|---|---|
| `ai-gateway-proxy` | Public (HTTPS) | Proxy only — the single ingress point |
| `ai-gateway-governance` | Internal (`*.internal`) | Governance + OPA — not reachable from the internet |
| `ai-gateway-db` | Internal | Managed Postgres (Fly Postgres or Supabase) |

OPA runs as a sidecar or co-deployed container alongside governance and is never exposed externally. The `GOVERNANCE_INTERNAL_TOKEN` ensures that even if the internal network were misconfigured, unauthenticated calls to `/inspect` are rejected with `403`.

Redis (rate limiter) is deployed as a Fly-managed Redis instance, accessible only within the private network.

---

## Portfolio Notes

This project is designed to be representative of production AI platform engineering work. Specifically, it demonstrates:

**LLM Infrastructure**
- OpenAI-compatible API surface with multi-provider routing (OpenAI, Anthropic, Gemini, Ollama, generic)
- Streaming support across all providers
- Model alias resolution and tier-based RBAC
- Per-user sliding-window rate limiting via Redis Lua scripts

**Governance and Compliance**
- GDPR-compliant PII detection at inference time using spaCy NER + Microsoft Presidio
- HMAC-keyed pseudonymization with rotation and right-to-erasure support
- HIPAA-aware PHI routing restrictions enforced at the policy layer
- Append-only partitioned audit log with UUID7 time-ordered IDs

**Policy as Code**
- OPA Rego policies with full unit test coverage (`make opa-test`)
- Deny-by-default with explicit allow — no implicit passthrough
- Hot-reload of policies without proxy restart

**Security Design**
- Fail-closed: governance or OPA unreachable = request denied
- PostgreSQL Row-Level Security with FORCE — tenant isolation enforced at the DB layer
- JWT + bcrypt API key dual auth with TTL-cached tenant context
- 1MB body size limit, CORS, and request ID propagation throughout

**Operational Readiness**
- Docker Compose local stack with health checks and ordered startup
- Idempotent IaC provisioner for tenants, users, and model config
- Alembic migrations with a dedicated migrate service
- Integration test suite runnable against the live stack (`make test-integration`)
- Makefile-first interface: `make up`, `make down`, `make demo`, `make logs`
