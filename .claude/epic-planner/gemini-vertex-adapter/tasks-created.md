# Tasks Created for Vertex AI Gemini Adapter (SA-authenticated, dual-path)

**Epic:** ai-gateway-76iq
**Date:** 2026-08-08

## Tasks

| ID | Title | Depends On | Ready |
|----|-------|------------|-------|
| ai-gateway-76iq.1 | Add Vertex AI settings fields and env config | - | Yes |
| ai-gateway-76iq.2 | Fix Gemini provider naming bug (google -> gemini) and remove compensating shim | - | Yes |
| ai-gateway-76iq.3 | Create shared Gemini translation core (_gemini_common.py) and refactor gemini.py | - | Yes |
| ai-gateway-76iq.4 | Add google-auth dependency and implement VertexCredentialManager | .1 | No |
| ai-gateway-76iq.5 | Implement Vertex Gemini adapter non-streaming chat_completions | .3, .4 | No |
| ai-gateway-76iq.6 | Implement Vertex Gemini adapter streaming (alt=sse) | .5 | No |
| ai-gateway-76iq.7 | Add Vertex-specific error classification to error handling | .5 | No |
| ai-gateway-76iq.8 | Extend routing.py and provider_capabilities.py for gemini-vertex | .2 | No |
| ai-gateway-76iq.9 | Add gemini-vertex model catalog entry and tenants.yaml validation | .8 | No |
| ai-gateway-76iq.10 | Extend rego policies (provider_override, allow_model, authz) for gemini-vertex | .8 | No |
| ai-gateway-76iq.11 | Wire main.py dispatch and startup client construction for gemini-vertex | .5, .9 | No |
| ai-gateway-76iq.12 | Add adapter unit and contract tests for gemini-vertex | .5, .6, .7 | No |
| ai-gateway-76iq.13 | Add routing, config, and policy tests for gemini-vertex selection | .9, .10, .11 | No |
| ai-gateway-76iq.14 | Update docs and add architecture diagram for Vertex Gemini adapter | .11, .12, .13 | No |

## Ready now (no blockers)
- ai-gateway-76iq.1 (settings/config)
- ai-gateway-76iq.2 (naming bug fix)
- ai-gateway-76iq.3 (shared translation core)

These three can be worked in parallel. Everything else unlocks progressively as the dependency chain above clears.

## Build-order rationale
Mirrors the SPEC's 8 implementation phases:
1. Config & naming fix -> .1, .2
2. Shared translation core -> .3
3. Credential manager & Vertex adapter -> .4, .5
4. Streaming -> .6
5. Error handling -> .7
6. Routing & selection -> .8, .9, .10, .11
7. Tests -> .12, .13
8. Docs -> .14

## Flagged risks embedded in task notes/AC (not blockers)
- ai-gateway-76iq.6 (streaming): exact `alt=sse` framing not confirmed against a canonical Google doc; AC requires a live smoke test before merge.
- ai-gateway-76iq.7 (error classification): exact 403 `reason` string for a missing Vertex AI IAM role grant inferred by analogy; AC requires verification against a real failure before relying on it in production.
