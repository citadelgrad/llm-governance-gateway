# Public release checklist

Recommended public repository name: `llm-governance-gateway`.

This repository is a public reference implementation for a governed LLM gateway. Before publishing, treat release as a security gate, not a README polish pass.

## Naming

Use `llm-governance-gateway` for the public repo.

Why:

- More specific than `ai-gateway`.
- Searchable by the actual problem domain.
- Matches the existing docs/plans language around LLM governance.
- Signals that the repo is about controls, not just request proxying.

Avoid:

- `ai-gateway` — too generic.
- `secure-ai-gateway` — vague marketing word, less precise.
- `policy-ai-proxy` — accurate but narrower than the implementation.
- `openclaw-*` or company/project-specific branding — unnecessary for a public reference repo.

## Required before making the repo public

1. Secret scan current tree.
2. Secret scan git history.
3. Rotate any real secrets that ever appeared in committed history.
4. Confirm `.envrc`, `.env`, local DB volumes, caches, and issue exports are not tracked.
5. Confirm example config uses placeholders, not key-shaped fake secrets.
6. Run quality gates:
   - `make test`
   - `make opa-test`
   - `make lint`
   - `make test-integration`
7. Decide on a license and add `LICENSE`.
8. Confirm Fly.io app names, tenant IDs, and demo emails are safe examples.
9. Create the public repo from clean intended files only if the private history has any uncertainty.

## Recommended secret-scan commands

If `gitleaks` is installed:

```bash
gitleaks detect --source . --redact
gitleaks detect --source . --no-git --redact
```

If the private history is questionable, publish a fresh-history snapshot instead of flipping this repository public.

## Fresh-history public snapshot pattern

1. Keep the original repo private.
2. Create a clean export directory outside this repo.
3. Copy only intended public files.
4. Exclude `.git`, `.beads`, `.envrc`, `.env`, caches, local state, DB volumes, recordings, private notes, and untracked artifacts.
5. Initialize a new git repository in the export directory.
6. Run secret scans in the export directory.
7. Run the same tests/checks from the export directory.
8. Create `github.com/<owner>/llm-governance-gateway` as public.
9. Push the new single-commit history.
10. Verify the remote URL, visibility, default branch, and pushed commit.

## Files intentionally public-safe

- `.envrc.example` contains placeholders only.
- `config/tenants.yaml` uses example tenant IDs and example contact domains.
- `config/users.yaml` uses placeholder initial keys.
- `docs/demo-scenarios.md` uses mock/demo request content.
- `policies/data/*.json` are generated policy data and should be reviewed before publish.

## Files/state that should stay private or ignored

- `.envrc`
- `.env`
- Provider API keys
- JWT/HMAC/internal tokens
- Local Postgres/Redis volumes
- `.beads/` runtime/export state unless intentionally publishing issue metadata
- `.pytest_cache/`, `.ruff_cache/`, `__pycache__/`
- Any personal deployment logs or app-specific Fly secrets

## License recommendation

Use MIT if the goal is maximum reuse with minimal friction.

Use Apache-2.0 if patent language matters.

Do not publish without a license unless the intent is "source visible, no explicit reuse rights." That's usually not what people mean by public repo.
