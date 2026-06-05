---
title: "fix: Resolve infrastructure config mismatches and dead code (gw-11d, gw-mbl, gw-twv, gw-1ex)"
type: fix
status: completed
date: 2026-06-04
---

# Fix Infrastructure Config Mismatches and Dead Code

Four small surgical bug fixes grouped into two waves. Changes were applied in commit `732f34a` (fix: harden gateway proxy test infrastructure). This plan verifies correctness and closes the open beads issues.

## Enhancement Summary

**Deepened on:** 2026-06-04
**Research agents used:** docker-compose env var best practices, Python Hypothesis test ordering, shell script secret detection patterns

### Key Improvements Discovered
1. `BEARER_PATTERN` in pre-commit-security.sh misses base64 chars (`+/=`) — gw-1ex fix is correct but incomplete
2. `sk-` pattern misses `sk_live_` and `sk-ant-` variants — worth a follow-up issue
3. Using `-P` (PCRE) instead of `-E` (ERE) would break on macOS BSD grep; current script uses `-E` correctly
4. Hypothesis strategy ordering is an import-time constraint, not just style — the gw-twv fix is semantically required

---

## Wave 1: Infrastructure Config (docker-compose.yml)

### gw-11d — Port fallback mismatch

**Bug:** docker-compose.yml used `${GATEWAY_PROXY_PORT:-8765}` and `${GATEWAY_POSTGRES_PORT:-15432}` as fallback defaults, but the Makefile and client code (gateway_client.py, provision.py) expected 18765/15433. Running `docker compose up` directly would bind the wrong ports.

**Fix applied:** `docker-compose.yml` proxy service ports now use `${GATEWAY_PROXY_PORT:-18765}:8000` and postgres service uses `${GATEWAY_POSTGRES_PORT:-15433}:5432`.

**Verify:**
```bash
grep 'GATEWAY_PROXY_PORT' docker-compose.yml    # must show :-18765
grep 'GATEWAY_POSTGRES_PORT' docker-compose.yml # must show :-15433
```

### Research Insights — Port Mapping

**Best Practices:**
- Never hardcode host ports; bind only the container port and let the host port come from env: `"${PORT:-18765}:8000"`
- Assign unique non-default ports per project; check conflicts: `lsof -i -P | grep LISTEN`
- The `:-` (dash-equals) form is preferred over `-` (dash only): `:-` applies the default when the variable is unset OR empty; `-` only when unset

**Edge Cases:**
- If two projects both default to 18765, `docker compose up` on either will conflict silently — document port in Makefile header comment
- Changing port defaults after `docker compose up` has run requires `docker compose down && docker compose up` — a restart alone won't rebind

---

### gw-mbl — Hardcoded DATABASE_URL password

**Bug:** governance and proxy services in docker-compose.yml hardcoded the password literal `gateway:gateway` inside the DATABASE_URL, making `POSTGRES_PASSWORD` env var overrides ineffective for application containers (postgres service was already correct).

**Fix applied:** All DATABASE_URL values now use `${POSTGRES_PASSWORD:-gateway}` interpolation consistently across migrate, governance, and proxy services.

**Verify:**
```bash
grep -n 'DATABASE_URL' docker-compose.yml
# Every line must show :${POSTGRES_PASSWORD:-gateway}@ — no literal :gateway@
```

### Research Insights — DATABASE_URL Credential Consistency

**Best Practices:**
- Build `DATABASE_URL` from the same variables that configure the postgres service — never separately. This eliminates the class of bug where DATABASE_URL drifts from POSTGRES_PASSWORD:
  ```yaml
  # Preferred: single source of truth
  DATABASE_URL: "postgresql+asyncpg://${POSTGRES_USER:-gateway}:${POSTGRES_PASSWORD:-gateway}@postgres:5432/${POSTGRES_DB:-gateway}"
  ```
- For credentials with no safe default (production secrets), use `:?` to abort compose-up with a descriptive error: `${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}`
- Never set DATABASE_URL statically in `.envrc` AND compose YAML — pick one location; compose YAML interpolation is preferred

**Pitfalls:**
- `POSTGRES_PASSWORD` only takes effect on first container creation. If you change the default after the postgres volume exists, the database ignores it. The `:?` form catches this mismatch early.
- Hardcoded credentials in application service DATABASE_URL that don't match the postgres service's POSTGRES_PASSWORD will cause silent connect failures that look like network issues

---

## Wave 2: Dead Code (tests + scripts)

### gw-twv — _MALFORMED_BYTES forward reference and dead code

**Bug:** In `apps/gateway-proxy/tests/test_properties.py`, `_MALFORMED_BYTES` (line ~169) referenced `_is_valid_json_object` before its definition (line ~174). This is an import-time constraint — Hypothesis resolves `@given` decorators and module-level strategy expressions at import time, so the definition order is semantically required, not just stylistic. Also, the issue noted _MALFORMED_BYTES appeared unused.

**Fix applied:** `_is_valid_json_object` is now defined above `_MALFORMED_BYTES`. The strategy IS used by `test_p1_binary_garbage_never_500` via `@given(raw=_MALFORMED_BYTES)`.

**Verify:**
```bash
grep -n '_is_valid_json_object\|_MALFORMED_BYTES' proxy/tests/test_properties.py
# _is_valid_json_object def must appear at a lower line number than _MALFORMED_BYTES =
# _MALFORMED_BYTES must appear in @given decorator
```

### Research Insights — Hypothesis Strategy Organization

**Best Practices:**
- Module-level strategy constants must be defined before they are referenced — Python resolves strategy expressions at import time. This makes the ordering an import-time constraint, not style.
- Use a single underscore prefix (`_MALFORMED_BYTES`) for module-level strategy helpers — most dead code analysis tools (vulture, flake8-unused) skip private names, avoiding false positive "unused variable" warnings.
- Use named module-level constants when: (1) the strategy is reused across 2+ tests, or (2) the name communicates intent better than the expression (e.g. `_MALFORMED_BYTES` is clearer than repeating the filter inline).
- Keep inline when the strategy is trivially readable and used in only one place.
- Extract to `tests/strategies.py` only when 3+ test files import the same strategy — premature extraction before reuse exists adds indirection with no benefit.

**Edge Cases:**
- If strategies are ever extracted to a separate module, `_is_valid_json_object` must be exported too (or inlined into `_MALFORMED_BYTES`) — the forward reference issue resurfaces in a different form
- `flake8-unused` and `vulture` may still flag `_MALFORMED_BYTES` as unused if they don't parse decorator arguments; the underscore prefix is the correct mitigation

---

### gw-1ex — BEARER_PATTERN unused in pre-commit-security.sh

**Bug:** `scripts/pre-commit-security.sh` defined `BEARER_PATTERN` at line 23 but `check_file()` never called grep with it — Bearer token literals hardcoded in source (e.g. hardcoded Authorization headers) would not be caught.

**Fix applied:** `check_file()` now includes a grep check using `$BEARER_PATTERN` that blocks commits with Bearer token literals, using `-E` (ERE) for cross-platform compatibility.

**Verify:**
```bash
grep -n 'BEARER_PATTERN' scripts/pre-commit-security.sh
# Must show both the definition AND a grep -qiE "$BEARER_PATTERN" usage inside check_file()
```

### Research Insights — Secret Detection Pattern Quality

**Pattern flag — current BEARER_PATTERN is incomplete:**

Current: `Bearer [a-zA-Z0-9_\-\.]{20,}`

Missing base64 padding characters (`+`, `/`, `=`) used in JWT and many OAuth tokens. Recommended improvement:
```bash
BEARER_PATTERN='Bearer [A-Za-z0-9+/=_\-\.]{20,}'
```

**Cross-platform grep flag:** The script correctly uses `-E` (POSIX ERE) rather than `-P` (PCRE). This is correct — `-P` is not available in BSD grep (macOS default) and would silently fail or error. Always use `-E` in cross-platform shell scripts.

**Other pattern gaps (follow-up issue):**
- `sk-` pattern misses `sk-ant-` (Anthropic) and `sk_live_` (Stripe) variants — `sk[-_][a-zA-Z0-9_\-]{20,}` is more complete
- No private key pattern: `-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY`
- No `token = "..."` variant distinct from the generic `GENERIC_SECRET_PATTERN`

**False positive mitigation already in place:** Skipping `*.example`, `*.sample`, `*.template` files is a correct and standard approach.

---

## Acceptance Criteria

- [x] `docker-compose.yml` proxy port fallback is `:-18765` (not `:-8765`)
- [x] `docker-compose.yml` postgres port fallback is `:-15433` (not `:-15432`)
- [x] All `DATABASE_URL` entries in `docker-compose.yml` use `${POSTGRES_PASSWORD:-gateway}` (no hardcoded `:gateway@`)
- [x] `_is_valid_json_object` defined before `_MALFORMED_BYTES` in `test_properties.py`
- [x] `_MALFORMED_BYTES` referenced in at least one `@given` decorator
- [x] `BEARER_PATTERN` is used inside `check_file()` in `pre-commit-security.sh` with `-E` flag
- [x] All four beads issues closed: gw-11d, gw-mbl, gw-twv, gw-1ex

## Follow-up Issues (out of scope for this plan)

- Improve `BEARER_PATTERN` to include base64 chars: `Bearer [A-Za-z0-9+/=_\-\.]{20,}`
- Expand `sk-` pattern to cover `sk-ant-` and `sk_live_` variants
- Add private key pattern to pre-commit-security.sh

## Files Changed

| File | Issue | Change |
|------|-------|--------|
| `docker-compose.yml` | gw-11d | Port fallback defaults: `8765→18765`, `15432→15433` |
| `docker-compose.yml` | gw-mbl | `DATABASE_URL` password: literal `gateway` → `${POSTGRES_PASSWORD:-gateway}` |
| `proxy/tests/test_properties.py` | gw-twv | Move `_is_valid_json_object` above `_MALFORMED_BYTES` |
| `scripts/pre-commit-security.sh` | gw-1ex | Wire `BEARER_PATTERN` into `check_file()` grep check |

## Sources

- beads issue gw-11d: port fallback mismatch
- beads issue gw-mbl: hardcoded DATABASE_URL password
- beads issue gw-twv: dead code / forward reference in test_properties.py
- beads issue gw-1ex: BEARER_PATTERN unused in pre-commit-security.sh
- Commit: `732f34a fix: harden gateway proxy test infrastructure (16 bugs)`
- [Hypothesis custom strategies docs](https://hypothesis.readthedocs.io/en/latest/tutorial/custom-strategies.html)
- [Docker Compose variable substitution](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
