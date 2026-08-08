# PRD: Vertex AI Gemini Adapter (SA-authenticated, dual-path)

**Status:** Approved
**Author:** Claude (epic-planner agent)
**Created:** 2026-08-08
**Beads Epic:** ai-gateway-76iq

---

## Overview

The gateway already routes Gemini traffic through one path: the public Gemini Developer API, authenticated with an API key (`proxy/app/providers/gemini.py`, `GEMINI_API_KEY`). Some deployments need a second path to the same Gemini models through **Google Cloud Vertex AI**, authenticated with a GCP **service account (SA)** instead of an API key — because Vertex AI gives org-level IAM control, project-scoped audit logs, VPC-SC support, and billing tied to a GCP project rather than a standalone API key.

This feature adds that second path as a genuinely optional, side-by-side alternative — not a replacement. Both paths must keep working after this ships. Admins and tenants choose which path a given model/request uses; the choice is explicit and auditable, not a hidden fallback.

This also fixes a live naming bug uncovered during design: `config/models.yaml` tags today's Gemini entries `provider: google`, while the rest of the codebase (`routing.py`, `provider_capabilities.py`) keys off `"gemini"`. `proxy/app/main.py` line 486 papers over the mismatch with a one-line shim. This feature removes that shim by correcting the label at the source.

## Goals

1. Add a Vertex AI-backed Gemini adapter, authenticated via an impersonated GCP service account (following the gateway's existing impersonated-ADC convention, not a raw SA key file).
2. Keep the existing API-key-based Gemini Developer API adapter fully working, unmodified in its external behavior.
3. Give admins/tenants/callers an explicit, layered way to choose which Gemini path a request uses: a distinct model-catalog entry, a per-tenant default, and a role-gated per-request header override.
4. Fix the `provider: google` vs `provider: gemini` naming inconsistency in `config/models.yaml` and remove the `main.py` shim that papers over it today.
5. Give both Gemini paths equivalent error-handling behavior through the shared `sanitize_upstream_error()` path, with Vertex-specific failure modes (expired token, missing impersonation grant, missing Vertex IAM role, model/region unavailable, quota exceeded) classified distinctly for internal logging even though the caller-facing message stays generically opaque per existing policy.
5. Document the new path (architecture doc update, README update, a dedicated Vertex-auth doc modeled on `docs/google-sensitive-data-protection.md`, and a C4-style Mermaid diagram) so operators can configure and reason about it.

### Non-Goals

- This feature does **not** add Vertex AI (or either Gemini path) to the PHI/BAA-approved provider list (`policies/llm/authz.rego`'s `phi_approved_providers`). PHI-eligible routing is out of scope; it would require a separate compliance review.
- This feature does **not** change or remove the existing Gemini Developer API adapter's request/response behavior for existing callers who don't opt into the Vertex path.
- This feature does **not** adopt Google's `google-genai` SDK as a runtime dependency for any adapter. It stays a dev-only contract-testing dependency, per the approved design recommendation.
- This feature does **not** build a new standalone credential-sentinel background service for the proxy (mirroring DLP's). It relies on `google-auth`'s built-in token-validity checking and per-request error handling instead; this call was made during planning and should be revisited only if operational experience shows it's insufficient.
- This feature does **not** cover fine-tuned/custom Vertex Endpoint models (`projects.locations.endpoints.generateContent`) — only stock `publishers/google/models/gemini-*` models.
- This feature does **not** implement the Vertex "global" endpoint as the default; global-endpoint support is a configuration option, not this feature's default behavior.

## User Stories

- **US-1.** As a platform admin, I want to configure a Vertex AI-backed Gemini model in the model catalog, so that requests for that model id are routed through the SA-authenticated path without touching any other model's configuration.
- **US-2.** As a platform admin, I want to set a tenant's default provider to the Vertex path, so that all of that tenant's Gemini-prefix requests use SA auth by default, without every caller needing to know a special model id.
- **US-3.** As an authorized caller with the right role, I want to override the provider for a single request via the existing `x-gateway-provider` header, so that I can force the Vertex path (or the API-key path) for one call without changing tenant or catalog configuration.
- **US-4.** As an operator, I want Vertex-specific auth failures (expired token, missing impersonation grant, missing Vertex IAM role) to be distinguishable in logs, so that I can tell "fix the impersonation grant" apart from "grant `roles/aiplatform.user`" without guessing from a generic 403.
- **US-5.** As a developer extending the gateway, I want the Vertex adapter's request/response translation to share code with the existing Gemini adapter where the two APIs agree, so that a bug fix to shared logic (e.g., token-usage field mapping) doesn't need to be applied twice.
- **US-6.** As a security reviewer, I want the Vertex adapter to authenticate only via impersonated ADC (never a raw SA key file), consistent with the gateway's existing Google credential conventions, so that credential handling stays auditable and consistent across the codebase.

## Functional Requirements

- **FR-1.** Add a new provider adapter module for Vertex AI Gemini (`proxy/app/providers/gemini_vertex.py`) implementing `make_client()` and `chat_completions()` with the same external call signature shape as the existing `gemini.py` adapter, so `main.py`'s dispatch code can treat it uniformly.
- **FR-2.** Extract the request/response translation logic shared by both Gemini paths (content/parts mapping, `generationConfig` mapping, tool/function-calling mapping, the overlapping subset of finish reasons and safety categories, usage-metadata mapping) into a shared module (`proxy/app/providers/_gemini_common.py`), parameterized by a small per-backend dialect object. Both `gemini.py` and `gemini_vertex.py` call into this shared module; neither duplicates the shared mapping logic.
- **FR-3.** The Vertex adapter must build the fully-qualified model resource path (`projects/{project}/locations/{location}/publishers/google/models/{model}`) and must not include a `model` field in the request body (Vertex's schema has none).
- **FR-4.** The Vertex adapter must handle the documented body-shape divergences distinctly from the Developer API adapter: the `blockReason` "unset" sentinel spelling difference (`BLOCKED_REASON_UNSPECIFIED` vs `BLOCK_REASON_UNSPECIFIED`), the non-overlapping `finishReason` enum members (Vertex has `MODEL_ARMOR`; the Developer API has `LANGUAGE`, `TOO_MANY_TOOL_CALLS`, `MISSING_THOUGHT_SIGNATURE`, `MALFORMED_RESPONSE`, `ESCALATION`), and the four-category overlap in `HarmCategory` safety settings (`HATE_SPEECH`, `DANGEROUS_CONTENT`, `HARASSMENT`, `SEXUALLY_EXPLICIT`).
- **FR-5.** Add credential acquisition and caching: load an impersonated-service-account ADC credential via `google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])`, cache the credential object for the process lifetime (constructed once in `make_client()`, not per-request), and refresh it via `credentials.refresh(...)` wrapped in `asyncio.to_thread` so the blocking call never runs on the event loop. Check `credentials.valid` before each outbound call rather than implementing custom expiry-margin logic (the library already applies a ~3m45s margin).
- **FR-6.** Add new `Settings` fields (`proxy/app/config.py`) for the Vertex path: GCP project id, region/location (with a documented option to use the `global` value), the ADC credentials file path (falls back to standard ADC discovery if unset), and the expected impersonated service-account identity (for startup validation, mirroring `scripts/google_adc_keychain.py`'s `--expected-service-account` pattern). Field/env-var names must be visually distinct from governance's existing `GOOGLE_DLP_*` settings (e.g. a `GEMINI_VERTEX_*` / `gemini_vertex_*` prefix) even where the underlying GCP project id value may coincide.
- **FR-7.** Fix `config/models.yaml`: change the existing Gemini entries' `provider: google` to `provider: gemini`. Remove the compensating shim at `proxy/app/main.py` line 486 (`capability_provider = "gemini" if provider == "google" else provider`). Add a new provider value, `gemini-vertex`, for the new path — never `google-vertex` or `vertex`.
- **FR-8.** Add at least one new model-catalog entry in `config/models.yaml` with `provider: gemini-vertex` (an explicit, discoverable model id/alias for the Vertex path), so a caller can select the Vertex path by requesting that model id directly, without touching tenant config or headers.
- **FR-9.** Confirm/extend `config/tenants.yaml`'s existing `default_provider` mechanism to accept `gemini-vertex` as a valid value, so a tenant can be defaulted onto the Vertex path for prefix-inferred `gemini-*` requests.
- **FR-10.** Confirm/extend `policies/llm/provider_override.rego` and any related model-tier policy (`policies/llm/allow_model.rego`) so the existing role-gated `x-gateway-provider` header override works for `gemini-vertex` as a recognized provider value, requiring the `gateway:provider_override:gemini-vertex` permission like other provider overrides.
- **FR-11.** Add a new capability entry to `proxy/app/provider_capabilities.py` for `gemini-vertex` (reusing/extending the existing Gemini capability field lists, adjusted for the documented divergences), so `unsupported_chat_fields()` continues to work correctly for the new path.
- **FR-12.** Route all Vertex adapter errors through `sanitize_upstream_error()`, extending it (or adding Vertex-specific classification ahead of it) to distinguish, for internal logging only: (a) expired/invalid Bearer token (401, `ACCESS_TOKEN_EXPIRED`), (b) missing impersonation grant, raised by `iamcredentials.googleapis.com` before Vertex is ever called (403), (c) missing `roles/aiplatform.user`-equivalent grant, raised by `aiplatform.googleapis.com` (403), (d) model/region unavailable (404), (e) quota exceeded (429, `rateLimitExceeded`). The caller-facing response stays generically opaque for these statuses, per the adapter's existing behavior for other providers.
- **FR-13.** Add `google-auth` as a new runtime dependency via `uv add` in `proxy/pyproject.toml`. Do not add `google-genai`, `google-cloud-aiplatform`, or any other Google SDK as a runtime dependency.
- **FR-14.** Add automated tests mirroring the existing patterns in `proxy/tests/test_adapters.py` (httpx-mocked request/response round-trips for the Vertex adapter, including at least one streaming test) and `proxy/tests/test_official_sdk_contracts.py` (field-list contract checks, extended to cover the Vertex-specific field divergences documented in research), plus routing/config tests (model-catalog selection, tenant default, header override) and a policy test for the `gemini-vertex` provider-override permission.
- **FR-15.** Update `docs/architecture.md` and `README.md` to describe the new path, and add a new doc (e.g. `docs/google-vertex-ai-gemini.md`) modeled on `docs/google-sensitive-data-protection.md`'s structure, including a C4-style Mermaid `flowchart` diagram (not the `C4Context`/`C4Container` plugin) and a `sequenceDiagram` for the runtime credential-refresh-and-call flow, saved under `docs/`.

## Success Criteria

- A request to a `gemini-vertex`-provider model completes successfully against a real (or realistically mocked) Vertex AI endpoint, returning a response translated into the same OpenAI-compatible envelope shape the existing Gemini adapter produces.
- The existing Gemini Developer API adapter's test suite (`test_adapters.py`, `test_official_sdk_contracts.py`) continues to pass unmodified in its existing-path assertions after this feature ships.
- `config/models.yaml` no longer contains `provider: google`; `proxy/app/main.py` no longer contains the `"gemini" if provider == "google" else provider` shim; both are grep-verifiable as absent.
- All three selection mechanisms (model-catalog entry, tenant default, header override) are independently demonstrated in tests to route a request to `gemini-vertex`.
- A simulated expired-token, missing-impersonation-grant, missing-Vertex-IAM-role, and quota-exceeded scenario each produce a distinct internal log classification while returning the same opaque caller-facing error shape as other providers' equivalent failures.
- `uv sync` succeeds with `google-auth` present as a runtime dependency and no `google-genai`/`google-cloud-aiplatform` runtime dependency added.
- `docs/architecture.md`, `README.md`, and the new Vertex-auth doc are updated and internally consistent (no stale references to the removed shim or the old `provider: google` value).

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Enum/field divergences between the two Gemini APIs (finish reasons, `blockReason` sentinel spelling, safety categories) cause silent misbehavior if translation code assumes full parity. | Shared translation core is parameterized by an explicit per-backend dialect object; tests exercise both dialects, not just one with the other assumed equivalent. |
| Two independent 403 causes (impersonation-grant failure vs. missing Vertex IAM role) get conflated, making Vertex auth failures hard to diagnose operationally. | Classify by which upstream host/service raised the error (`iamcredentials.googleapis.com` vs `aiplatform.googleapis.com`) for internal logging; caller-facing response stays opaque per existing policy. |
| Blocking token refresh (`credentials.refresh()`) runs on the event loop and stalls the service under load. | Wrap all refresh calls in `asyncio.to_thread`; cache the credential object at client-construction time, not per-request. |
| Exact `alt=sse` streaming framing was not confirmed against a single canonical Google doc during research (flagged UNCERTAIN). | Add a live smoke test against the real Vertex streaming endpoint before the streaming path ships; do not assume framing matches the Developer API without verification. |
| Exact 403 `reason` string for a missing `roles/aiplatform.user`-equivalent grant was inferred by analogy, not directly confirmed. | Treat this as a verify-during-implementation item (per coordinator's explicit instruction); do not hardcode a `reason` string match without confirming it against a real failure during implementation/testing. |
| New env-var naming collides conceptually with governance's `GOOGLE_DLP_*` settings, causing operator confusion, given both likely point at the same GCP project. | Use a distinct `GEMINI_VERTEX_*` prefix for all new proxy-side settings; document explicitly that the two services configure Google Cloud access independently even when the project id value matches. |
| 404 "model not found" on Vertex is ambiguous (conflates missing model, wrong region, missing access) and can't be reliably parsed. | Validate model/region availability against the model catalog client-side rather than parsing Vertex's error text; document this limitation. |
| Adding a second Gemini path increases the model catalog's/config's complexity, risking operator misconfiguration (e.g. wrong provider on a model entry). | New model-catalog entries and provider values are validated at startup (existing `load_models_yaml()` pattern); document the three selection mechanisms clearly in the new Vertex-auth doc. |
