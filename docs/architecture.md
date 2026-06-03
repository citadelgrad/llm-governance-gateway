# Architecture

LLM Governance Gateway is a split-control-plane LLM proxy. The proxy owns client-facing compatibility and provider dispatch. The governance service owns safety, policy, audit, and data handling decisions.

## Components

| Component | Tech | Exposure | Responsibility |
|---|---|---|---|
| Proxy | FastAPI, asyncpg, httpx, Redis client | Public HTTP in local/dev; public HTTPS in deployment | OpenAI-compatible API, authentication, tenant lookup, routing, rate limiting, provider dispatch |
| Governance | FastAPI, Presidio, spaCy, llm-guard, asyncpg | Internal only | PII detection, pseudonymization, harm scoring, OPA policy calls, audit writes |
| OPA | Open Policy Agent | Internal only | Rego policy decisions for model tiers, PHI/provider restrictions, provider overrides |
| Postgres | Postgres 16 | Internal only | Audit log, pseudonym map, erasure log, bootstrap/provisioning state |
| Redis | Redis 7 | Internal only | Sliding-window rate-limit counters |
| Provider adapters | Python modules in `proxy/app/providers/` | Outbound only | OpenAI, Anthropic, Gemini, Ollama, generic OpenAI-compatible, mock provider |

## Request lifecycle

1. Client sends `POST /v1/chat/completions` to the proxy.
2. Proxy authenticates the caller with JWT/API-key logic.
3. Proxy loads tenant config and resolves the requested model/provider.
4. Proxy checks Redis rate limits before governance work.
5. Proxy sends request text plus caller/model metadata to governance.
6. Governance runs PII detection and redaction/pseudonymization.
7. Governance runs harm checks.
8. Governance sends policy input to OPA.
9. Governance writes an audit row with decision metadata.
10. Proxy blocks or forwards the possibly-redacted provider request.
11. Proxy returns the provider response plus audit/rate-limit/PII headers.

## Fail-closed model

The system intentionally fails closed:

- Proxy returns `503 governance_unavailable` if governance cannot be reached.
- Governance blocks when OPA cannot return a policy decision.
- OPA policies default to deny and require explicit allow rules.
- PHI routing is denied unless the provider is in the approved BAA set.

This is the right default for a governance gateway. Availability loss is annoying; silent policy bypass is worse.

## PII and audit boundaries

PII handling is designed so sensitive raw values do not leave the governance boundary unless tenant policy explicitly permits it.

- Presidio/spaCy detect PII entities.
- Detected values are replaced before provider dispatch when redaction is enabled.
- Audit records include PII entity metadata such as type, offset, and score.
- Audit records do not store the matched raw PII text.
- Pseudonyms are HMAC-derived using `PSEUDONYM_HMAC_KEY`.
- Right-to-erasure destroys the real-user-to-pseudonym link while keeping audit records for compliance.

## Policy model

Policy files live in `policies/llm/`.

Important policy themes:

- `authz.rego` handles core allow/deny behavior.
- `allow_model.rego` handles model-tier access.
- `provider_override.rego` controls whether callers can force provider routing.
- Parity tests keep duplicated model-tier maps aligned.

Run policy tests with:

```bash
make opa-test
```

## Routing model

Model routing config lives in `config/models.yaml`.

Each model has:

- `id` — requested model name.
- `provider` — provider adapter.
- `base_url` — upstream base URL when applicable.
- `alias_of` — optional canonical model ID.

Tenant defaults and allowed models live in `config/tenants.yaml`. Demo users live in `config/users.yaml`.

## Local deployment

Docker Compose starts:

- Postgres on host port `15432`.
- Proxy on host port `8765`.
- Internal governance service.
- Internal OPA service.
- Internal Redis service.
- Migration job.

Use:

```bash
make up
make provision
make demo
```

## Production deployment shape

The optional Fly.io examples in `infra/example/fly.io/` are intended for a split topology:

- Public proxy app is the only internet-facing service.
- Governance and OPA stay on private networking.
- Postgres and Redis stay private.
- Rotation/maintenance work runs through cron/one-off jobs.

Do not deploy governance or OPA as public services. That would be missing the point with confidence.
