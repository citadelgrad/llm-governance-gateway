---
date: 2026-05-26
topic: llm-governance-gateway
---

# LLM Governance Gateway

## What We're Building

An enterprise-grade LLM governance gateway that sits between consuming applications and external LLM providers. The system intercepts every LLM request, runs it through a governance pipeline (PII detection, compliance policy evaluation, harm/content classification), produces a full audit trail, and either passes the request through to the target provider or blocks/redacts it.

The architecture is split into two services: a **reference proxy** (FastAPI, OpenAI-compatible API surface) and a **governance engine** (FastAPI, ext_authz-style inspection API). The proxy calls the governance engine per-request. Any future proxy implementation — Envoy, nginx + Lua, Rust — can replace the reference proxy by implementing the same governance engine API contract.

## Target Audience

Enterprise platform/security teams with compliance requirements (GDPR, HIPAA, SOC2). This is a portfolio project targeting AI platform engineer, MLOps, and platform engineering roles.

## Architecture

```
Client App
    │
    ▼
Reference Proxy (FastAPI)          ← swappable with Envoy / Rust / nginx
    │  calls /inspect per request
    ▼
Governance Engine (FastAPI)
    ├── PII Detector (presidio)
    ├── Harm Classifier (transformers / rules)
    ├── OPA Policy Agent (ext_authz)
    └── Audit Logger (Postgres)
    │
    ▼ (if allowed + redacted)
Provider Router
    ├── OpenAI adapter
    ├── Anthropic adapter
    ├── Gemini adapter
    ├── Ollama adapter
    └── OpenAI-compatible adapter (generic)
```

**Governance Engine inspection API contract:**
```
POST /inspect
{
  "request_id": "...",
  "provider": "openai",
  "model": "gpt-4o",
  "messages": [...],
  "caller": { "user_id": "...", "tenant_id": "...", "roles": [...] }
}

Response:
{
  "decision": "allow" | "block" | "redact",
  "redacted_messages": [...],   // present if decision == "redact"
  "violations": [...],          // policy violations that fired
  "pii_findings": [...],        // detected PII types and positions
  "harm_score": 0.0–1.0,
  "audit_id": "..."
}
```

## Core Governance Pillars

1. **PII / Data Redaction** — Microsoft Presidio for NLP-based entity detection (names, SSNs, credit cards, emails, PHI). Configurable action per entity type: redact, block, or pass with audit flag.

2. **Compliance Policy Enforcement** — OPA (Open Policy Agent) evaluates access/compliance decisions using Rego policies. Examples: "PHI cannot be sent to non-HIPAA providers", "model X is blocked for tenant Y", RBAC-based model access.

3. **Content / Harm Detection** — Prompt injection detection, jailbreak pattern matching, harm classification (violence, illegal activity, CSAM). Hybrid: fast regex rules + ML classifier for ambiguous cases.

4. **Audit Logging & Observability** — Every request logged to Postgres with: caller identity, provider/model, PII findings, policy decisions, harm score, latency, outcome. Queryable audit API.

## Policy Architecture (Hybrid)

- **OPA** handles allow/deny/flag decisions for access control and compliance (which models, which data, which tenants). Policies versioned as `.rego` files in git.
- **YAML** handles governance pipeline configuration: which detectors to run, PII entity thresholds, harm score cutoffs, routing rules, provider credentials.

```yaml
# governance.yaml
pipeline:
  pii:
    enabled: true
    entities: [PERSON, SSN, CREDIT_CARD, PHI]
    action: redact          # redact | block | flag
  harm:
    enabled: true
    threshold: 0.75
    action: block
  opa:
    enabled: true
    bundle: ./policies/
```

## Provider Support

OpenAI-compatible interface as the common abstraction. Provider adapters translate to each backend:

| Provider | Adapter Notes |
|---|---|
| OpenAI | Native — reference implementation |
| Anthropic Claude | Adapter: messages format translation |
| Google Gemini | Adapter: Vertex AI / Gemini API |
| Ollama | Adapter: local endpoint, OpenAI-compatible |
| Generic OpenAI-compatible | Pass-through with base URL config |

## Tech Stack

| Component | Technology |
|---|---|
| Reference Proxy | Python 3.12, FastAPI, uvicorn |
| Governance Engine | Python 3.12, FastAPI, uvicorn |
| PII Detection | Microsoft Presidio + spaCy |
| Policy Engine | OPA (sidecar, HTTP API) |
| Harm Classification | Transformers (local) + regex rules |
| Audit Storage | Postgres (via SQLModel / asyncpg) |
| Rate Limiting | Redis |
| Package Management | uv |
| Containers | Docker Compose (dev) |
| Hosting | Fly.io or Railway (demo) |

## Key Decisions

- **Sidecar governance service over monolith**: Clean API contract makes the reference proxy fully swappable. Mirrors Envoy ext_authz — a real enterprise pattern.
- **FastAPI for both services**: Consistent stack, strong async support, automatic OpenAPI docs useful for the portfolio.
- **OPA for access/compliance, YAML for pipeline config**: OPA handles the complex conditional logic it was designed for; YAML stays simple for operational config.
- **Presidio for PII**: Microsoft-maintained, production-proven, supports custom entity recognizers — no need to reinvent this.
- **OpenAI-compatible as the proxy API surface**: Consumers don't need to change their client code to use the gateway.
- **uv only for Python deps**: Per project conventions.
- **Non-default ports**: Port selection TBD — avoid 8000/3000/5000.

## Resolved Questions

- **Audit UI**: REST API + FastAPI auto-generated OpenAPI docs (`/docs`). No custom frontend needed — reviewers explore and query the audit log interactively in the browser.
- **Rate limiting**: Per API key + per user. Keys mapped to users/tenants in DB; Redis tracks request counts with sliding window.
- **Harm classification**: Rules-only (regex + known jailbreak/illegal pattern matching) to start. Harm classifier is designed as a pluggable interface so an external API (OpenAI moderation, etc.) or local ML model can be swapped in later without changing the governance engine.
- **Authentication**: API keys for machine clients (`X-API-Key` header) + JWT bearer tokens for user-context calls. Keys and JWT claims both resolve to a `caller` context (user_id, tenant_id, roles) passed to OPA.
- **Port assignments**: TBD during implementation — will avoid all default ports (8000, 3000, 5000, 8080).

## Open Questions

- None — ready to plan.

## Next Steps

→ `/ce:plan` for implementation details and phased build order
