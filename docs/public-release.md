# Public Release Checklist

This repo is being prepared for public release as `llm-governance-gateway`.

## Completed baseline

- Apache-2.0 license added.
- `SECURITY.md`, `CONTRIBUTING.md`, and `CODE_OF_CONDUCT.md` added.
- GitHub Actions CI added for lint/type check, tests, OPA policy tests, integration smoke tests, secret scan, and whitespace checks.
- Python type checking standardized on `ty`.
- Example env/provider placeholders changed to scanner-friendly non-key-shaped values.
- Beads JSONL exports are ignored and should not be committed.

## Required before flipping an existing repo public

1. Run full local gates:

   ```bash
   make lint
   make test
   make opa-test
   make test-integration
   gitleaks detect --source . --redact --no-banner
   git diff --check
   ```

2. Review tracked internal artifacts:

   - `.claude/settings.json`
   - `AGENTS.md`
   - `CLAUDE.md`
   - `.beads/*`
   - `docs/brainstorms/*`
   - `docs/superpowers/*`

3. Confirm whether to publish existing git history or create a fresh-history public snapshot.

   If any real credentials, tenant data, private planning notes, or sensitive customer content ever existed in history, do not flip this repository public. Create a clean public export repo instead.

4. Decide public issue tracker strategy.

   Beads is fine locally, but public contributors usually expect GitHub Issues or a clearly documented alternative.

5. Add branch protection after pushing to GitHub:

   - require CI to pass
   - require PR review for `main`
   - block force pushes
   - enable Dependabot or Renovate

## Publication recommendation

Use a fresh public repository named `llm-governance-gateway` unless the private history has been intentionally audited and approved for public exposure.
