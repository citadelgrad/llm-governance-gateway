# Security Policy

AI Gateway is a security-sensitive LLM proxy: it handles prompts, provider credentials, API keys, policy decisions, PII redaction, and audit logs. Treat deployments accordingly.

## Supported Versions

This project is pre-1.0. Security fixes land on `main` until a formal release policy exists.

## Reporting a Vulnerability

Do not open public issues for suspected vulnerabilities.

Report privately to the repository owner/security contact. Include:

- affected commit/version
- reproduction steps
- impact
- relevant logs with secrets redacted
- whether any credentials, prompts, tenant data, or audit data may have been exposed

## Deployment Security Notes

- Set strong unique values for `JWT_SECRET`, `GOVERNANCE_INTERNAL_TOKEN`, `GATEWAY_BOOTSTRAP_TOKEN`, and `PSEUDONYM_HMAC_KEY`.
- Never commit real provider keys or generated `.envrc` / `.env` files.
- Keep Governance, OPA, Redis, and Postgres on private networks. The proxy is the only service intended to be public-facing.
- Do not expose the local Docker Compose stack directly to the public internet.
- Use mock providers for demos/tests; use real provider credentials only in trusted environments.
- Audit logs may contain pseudonymized or governance-processed prompt metadata. Confirm retention and access policies before production use.

## Secret Scanning

Run before publishing or pushing sensitive work:

```bash
gitleaks detect --source . --redact --no-banner
```

For current-tree checks that intentionally include ignored local files, use:

```bash
gitleaks detect --source . --no-git --redact --no-banner
```

Ignored local agent worktrees and caches are not part of a public export, but real findings in tracked files or git history must be fixed before release.
