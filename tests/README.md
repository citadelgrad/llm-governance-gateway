# Tests

## OPA Policy Tests

Run with: `make opa-test`

**35 tests across 5 test files** (OPA v1 syntax, `rego.v1` import required):

| File | Tests | What it covers |
|------|-------|----------------|
| `policies/llm/authz_test.rego` | 14 | Tier-1/tier-2 model access; **CRITICAL** deny+allow coexistence for PHI on unapproved provider; PHI gate for approved providers (azure-openai, bedrock); redact_pii signal |
| `policies/llm/audit_scope_test.rego` | 6 | PLATFORM/TENANT/SELF scope mapping; platform_admin beats tenant_admin; unknown roles default to SELF |
| `policies/llm/allow_model_test.rego` | 8 | Tenant allowlist gate for tier-1 and tier-2 models; unknown models denied |
| `policies/llm/provider_override_test.rego` | 6 | Per-provider permission scoping; **CRITICAL** anthropic permission cannot pivot to gemini |
| `policies/llm/model_tiers_parity_test.rego` | 1 | authz.rego and allow_model.rego model_tiers maps are identical |

### Critical test: deny+allow coexistence

`test_phi_unapproved_provider_deny_allow_coexist` asserts that when PHI is routed to a non-HIPAA-BAA provider by a tier2-access user, **both** conditions hold simultaneously:

- `allow == true` (the user has valid tier2 access)
- `deny` is non-empty (PHI cannot reach an unapproved provider)

The pipeline must treat a non-empty `deny` set as a hard block regardless of `allow`.
