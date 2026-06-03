# Security Policy

## Supported use

This project is a reference implementation for an LLM governance gateway. It is not safe to expose without real secrets, private networking, tenant review, and provider-specific compliance checks.

The proxy may be public. Governance, OPA, Postgres, and Redis should stay private/internal.

## Reporting vulnerabilities

If this repo is published under an organization, report vulnerabilities through that organization's preferred private channel or GitHub Security Advisories.

Do not open public issues containing:

- real API keys or tokens
- JWT secrets
- HMAC pseudonymization keys
- tenant/user identifiers from production systems
- request payloads containing PII, PHI, or customer data
- audit records from real deployments

## Secret handling

Never commit:

- `.envrc` or `.env`
- provider API keys
- `JWT_SECRET`
- `GOVERNANCE_INTERNAL_TOKEN`
- `GATEWAY_BOOTSTRAP_TOKEN`
- `PSEUDONYM_HMAC_KEY`
- production `DATABASE_URL` or `REDIS_URL`
- Fly.io secrets or deploy tokens

Use `.envrc.example` only as a template. It intentionally contains non-secret placeholders.

## Deployment warnings

- Keep governance and OPA off the public internet.
- Use private networking for Postgres and Redis.
- Rotate pseudonymization keys deliberately; rotation affects correlation semantics.
- Treat audit data as sensitive even when raw PII is not stored.
- Validate provider compliance before allowing PHI or regulated data to leave the system.
- Keep fail-closed behavior intact. Do not add fallback provider passthroughs when governance is unavailable.

## Pre-release checks

Before publishing or deploying from this repo:

```bash
make test
make opa-test
make lint
make test-integration
```

Also run current-tree and git-history secret scans. See `docs/public-release.md`.
