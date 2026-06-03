# Contributing

Thanks for improving AI Gateway.

## Local Setup

Prerequisites:

- Docker + Docker Compose v2
- Python 3.12+
- uv
- direnv, or equivalent manual environment loading

```bash
cp .envrc.example .envrc
# edit .envrc with local-only values; never commit real secrets
direnv allow
make up
make test
make test-integration
make opa-test
make lint
```

## Development Rules

- Keep real credentials out of git.
- Prefer mock providers for tests and demos.
- Add tests for behavior changes.
- Keep policy changes covered by OPA tests.
- Use `ty` for Python type checking; do not add pyright back.
- Do not commit Beads JSONL exports. Beads state is local/Dolt-backed; `.beads/*.jsonl` files are passive exports.

## Pull Request Checklist

Before opening a PR, run:

```bash
make lint
make test
make test-integration
make opa-test
gitleaks detect --source . --redact --no-banner
git diff --check
```

PRs should explain:

- what changed
- why it changed
- how it was tested
- any security, privacy, audit, or policy impact
