# Deep Research: Google Cloud Vertex AI Gemini API (August 2026)

**Purpose:** Feed a technical design spec for a Vertex AI-backed adapter in an existing LLM gateway (Python/FastAPI/httpx) that already has a working Gemini Developer API (API-key) adapter.

**Research date:** 2026-08-07

**Methodology:** Live verification against current Google Cloud documentation, Google's own REST Discovery documents (`$discovery/rest`), the `googleapis/googleapis` proto source, PyPI JSON API, and `google-auth` library source. Training-data memory was not relied upon for any endpoint shape, field name, or version number — all such claims below are cited to a live-fetched source. Findings marked **UNCERTAIN** could not be verified to high confidence in this pass and should be independently re-checked before the spec is finalized.

**Tooling note:** Most `docs.cloud.google.com/vertex-ai/...` pages render their body content client-side via JavaScript and returned only navigation chrome to a plain HTTP fetch. Where this happened, findings were instead verified against Google's machine-generated REST Discovery documents (`https://aiplatform.googleapis.com/$discovery/rest?version=v1` and `?version=v1beta1`, and `https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta`) and the `googleapis/googleapis` proto source — both authoritative, current, and immune to JS-rendering issues — plus corroborating real-world error bodies from GitHub issues and Google's developer forums. Any finding resting only on doc-page prose that could be fetched is noted as such.

---

## 1. Endpoint shape for `generateContent` on Vertex AI

### 1a. Regional endpoint — confirmed current

```
POST https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent
POST https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:streamGenerateContent
```

This is still the correct pattern today. Confirmed directly from the live Vertex AI Discovery document (`https://aiplatform.googleapis.com/$discovery/rest?version=v1`, revision `20260801`), method `aiplatform.projects.locations.publishers.models.generateContent`, with `flatPath: v1/projects/{projectsId}/locations/{locationsId}/publishers/{publishersId}/models/{modelsId}:generateContent`. The `model` path parameter is schema-validated against the pattern `^projects/[^/]+/locations/[^/]+/publishers/[^/]+/models/[^/]+$`.

A second, parallel resource path exists for **fine-tuned models deployed to a Vertex Endpoint** (as opposed to a bare publisher model): `projects.locations.endpoints.generateContent` / `...streamGenerateContent`, `flatPath: v1/projects/{projectsId}/locations/{locationsId}/endpoints/{endpointsId}:generateContent`. Relevant only if the adapter needs to call custom-tuned models rather than stock `gemini-*` publisher models.
Source: `https://aiplatform.googleapis.com/$discovery/rest?version=v1`; cross-confirmed by `google/cloud/aiplatform/v1/prediction_service.proto` in `googleapis/googleapis`, and the doc page `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1/projects.locations.publishers.models/generateContent`.

The Discovery document's `endpoints` array lists 44 region-specific hostnames of the form `https://{region}-aiplatform.googleapis.com/`. It also lists two additional entries that are **not** the global endpoint: `https://aiplatform.us.rep.googleapis.com/` (location `us`) and `https://aiplatform.eu.rep.googleapis.com/` (location `eu`) — apparently multi-region replicated endpoints. **UNCERTAIN**: exact semantics of the `.rep.` hosts were not verified; follow up only if multi-region (not single-region, not global) support is needed.

### 1b. v1 vs v1beta1

Both exist as fully separate, same-revision-date (`20260801`) Discovery documents with an independent type namespace (`GoogleCloudAiplatformV1GenerateContentRequest` vs `GoogleCloudAiplatformV1beta1GenerateContentRequest` — not v1 + a small delta). Both expose an identical `generateContent`/`streamGenerateContent` method surface, differing only in the URL version segment.

For a **new** adapter, v1 is the GA-labeled surface and has full parity with v1beta1 on the core `generateContent` path as far as verified. **UNCERTAIN**: no exhaustive field-by-field v1-vs-v1beta1 diff was performed; if the design later needs a v1beta1-exclusive capability, that requires dedicated follow-up.

Separately, for the unified `google-genai` SDK (see §6), **v1beta is the client's default API version**; stable v1 must be explicitly requested via `http_options=types.HttpOptions(api_version='v1')`. This is an SDK-level default, not a statement about which REST version Google recommends for hand-rolled callers — no primary-source recommendation of v1-over-v1beta1 (or vice versa) specifically for direct REST callers was found. **UNCERTAIN.**
Source: `https://raw.githubusercontent.com/googleapis/python-genai/main/README.md`.

### 1c. Global (non-regional) endpoint — confirmed to exist

- **Hostname:** `aiplatform.googleapis.com` with no region prefix. This is literally the Discovery document's `rootUrl`/`baseUrl` — the "global endpoint" is not a separate hostname in Google's system, it is the plain unprefixed host.
- **Project and location still appear in the path**; the location segment's value becomes the literal string `global`:
  ```
  https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/{model}:generateContent
  ```
  Example confirmed via Google's docs: `https://aiplatform.googleapis.com/v1/projects/test-project/locations/global/publishers/google/models/gemini-2.0-flash-001:generateContent`.
  Source: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations` (fetched via reader proxy). **Medium confidence** on the exact path string — corroborated by multiple independent sources but not confirmed via a second verbatim primary-source quote in this pass.
- **When to use it:** the global endpoint gives higher availability and materially reduces `429 RESOURCE_EXHAUSTED` errors because it is not bound to a single region's quota pool. Google explicitly warns you cannot control or know which region a global-endpoint request's ML processing lands in — do not use it if the design has data-residency / "ML processing region" control requirements.
  Sources: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations`; `https://cloud.google.com/blog/products/ai-machine-learning/reduce-429-errors-on-vertex-ai` (directly fetched — confirms the 429-reduction rationale as Google's own stated purpose for the global endpoint).
- **Exclusions:** fine-tuning/tuning jobs, batch prediction (at least for certain partner models), and RAG corpus operations are documented as **not** supported via the global endpoint — those require a regional endpoint.
- **Model coverage:** reported to extend to Gemini 3 Flash, Gemini 3 Pro, Gemini 2.5 Pro, Gemini 2.5 Flash, Gemini 2.0 Flash "and others" — no single complete itemized list was obtained. **UNCERTAIN** — treat global-endpoint support as "assumed available for current-generation Gemini models, verify per model/region at call time rather than hardcoding a list."
- **GA date:** the global endpoint reached general availability around **May 2, 2025**, per the Vertex AI Gemini release notes.
  Source: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/release-notes`.
- The Discovery document's own machine-generated `endpoints` array does **not** list a separate "global" entry — expected, since global is simply the default no-prefix `rootUrl` combined with `locations/global` in the path, not a discovery-listed endpoint variant.

### 1d. Streaming response framing — Vertex vs. Gemini Developer API

Both APIs behave **identically** here — same toggle, same two framings, confirmed via both Discovery documents' shared `alt` query parameter definition:

- **Default (no `?alt=sse`):** `Content-Type: application/json`. The body is a single JSON **array** (`[ {...}, {...}, ... ]`), each element a full `GenerateContentResponse` object, delivered incrementally via HTTP chunked transfer as the array grows. This is not newline-delimited JSON (NDJSON) — it is one progressively-growing JSON array.
- **With `?alt=sse` appended to the URL:** `Content-Type: text/event-stream`. Each chunk is a standard Server-Sent Event: `data: {...json...}\n\n`, one `GenerateContentResponse` object per `data:` line.
- This applies identically to Vertex's `:streamGenerateContent` and the Developer API's `:streamGenerateContent`.

Confidence note: `alt=sse` is a special-cased streaming mode layered onto `streamGenerateContent` — the Discovery documents' formal `alt` parameter enum only lists `json`, `media`, `proto` (identically on both APIs), so `sse` doesn't appear as a declared enum value; it works in practice but isn't declared in the machine schema. **Rated HIGH confidence, not primary-source-certain**: no single canonical Google prose page could be fetched with body content confirming this (the relevant `docs.cloud.google.com`/`ai.google.dev` streaming guide pages returned only navigation chrome). Convergent corroboration came from `https://ai.google.dev/api/generate-content` (fetched successfully, describes `?alt=sse` for the Developer API) and multiple independent third-party production engineering sources hitting this against both APIs: `https://github.com/BerriAI/litellm/issues/15293`, `https://github.com/BerriAI/litellm/issues/27444`, `https://github.com/musistudio/claude-code-router/issues/1315` ("vertex-gemini transformer missing ?alt=sse for streaming requests"), `https://github.com/cline/cline/issues/918`. **Recommend one live smoke test against both APIs before finalizing the SSE parser.**

---

## 2. Request/response body shape parity with the Gemini Developer API

All fields below come from a direct diff of the two live Discovery documents' JSON schemas (Vertex `GoogleCloudAiplatformV1*` types vs. Developer API bare-named types), cross-checked against proto source where noted.

### 2a. Model naming/addressing — the largest structural divergence

- **Vertex:** the model is identified **only via the URL path**, as a fully-qualified resource name: `projects/{project}/locations/{location}/publishers/google/models/{model}` (or `.../endpoints/{endpoint}` for tuned models). The `GenerateContentRequest` **JSON body has no `model` field at all** (confirmed absent from the Vertex request schema's top-level properties).
- **Developer API:** model is addressed via URL path too (`models/{model}`, `tunedModels/{model}`, or `dynamic/{model}`), but the request schema **also declares a `model` string property** (format `models/{model}`) in the JSON body — redundant with the path in normal usage but present in the schema.
- **Adapter impact:** where the existing Developer API adapter presumably sends a bare `gemini-2.0-flash` (or `models/gemini-2.0-flash`) string, the Vertex adapter must build a full resource path `projects/{project}/locations/{location}/publishers/google/models/{model}` and must not put `model` in the JSON body.

Source: live diff of `https://aiplatform.googleapis.com/$discovery/rest?version=v1` vs `https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta`.

### 2b. Request top-level fields

```
Vertex-only:     labels, modelArmorConfig
Dev-API-only:    model, serviceTier, store
Shared:          contents, systemInstruction, generationConfig, tools, toolConfig,
                 safetySettings, cachedContent
```
- `labels` (Vertex-only): `Map<string,string>` — standard GCP resource labels for billing/cost attribution. No Developer API equivalent.
- `modelArmorConfig` (Vertex-only): `GoogleCloudAiplatformV1ModelArmorConfig` — hooks into Google Cloud's Model Armor content-safety product. No Developer API equivalent.
- `serviceTier` (Dev-API-only request field): enum `unspecified|standard|flex|priority`. Vertex has no equivalent *request*-level field, but Vertex's *response* `UsageMetadata` carries a mirroring `serviceTier` value (§2l) — Vertex has a separate priority/quota mechanism (`trafficType`) instead.
- `store` (Dev-API-only, boolean): purpose not chased down. **UNCERTAIN** — verify if the adapter needs Developer-API conversation-storage parity.

### 2c. `Content` / `Part`

`Content` (`role`, `parts`) is **identical** on both sides.

`Part` field-name diff:
```
Vertex-only:   executableCode*, thoughtSignature*, functionResponse*, audioTranscription,
               inlineData*, codeExecutionResult*, fileData*, text*, functionCall*, thought*,
               mediaResolution*, videoMetadata*
Dev-API-only:  toolCall, toolResponse, partMetadata
(* = also present on Dev API under the identical name — see "shared" below)
```
Shared (identical field names both sides): `text`, `inlineData`, `fileData`, `functionCall`, `functionResponse`, `executableCode`, `codeExecutionResult`, `thought`, `thoughtSignature`, `mediaResolution`, `videoMetadata`.
Divergences: Vertex has `audioTranscription` with no found Developer API counterpart under that name; Developer API has `toolCall`, `toolResponse`, `partMetadata` with no Vertex counterpart.

### 2d. `systemInstruction`

Identical shape and field name on both APIs — a `Content` object.

### 2e. `GenerationConfig`

```
Vertex-only:    audioTimestamp, routingConfig
Dev-API-only:   _responseJsonSchema, enableEnhancedCivicAnswers, translationConfig
Shared:         audioTranscriptionConfig, candidateCount, enableAffectiveDialog, frequencyPenalty,
                imageConfig, logprobs, maxOutputTokens, mediaResolution, presencePenalty,
                responseFormat, responseJsonSchema, responseLogprobs, responseMimeType,
                responseModalities, responseSchema, seed, speechConfig, stopSequences,
                temperature, thinkingConfig, topK, topP
```
- `routingConfig` (Vertex-only): Vertex's model auto-routing feature. No Developer API equivalent.
- `audioTimestamp` (Vertex-only, boolean): request word-level timestamps for audio output.
- `translationConfig` / `enableEnhancedCivicAnswers` (Dev-API-only): no Vertex equivalent found.
- The core sampling/generation-control parameters (`temperature`, `topP`, `topK`, `maxOutputTokens`, `stopSequences`, `responseMimeType`/`responseSchema`, `thinkingConfig`, `seed`, etc.) are field-name-identical across both APIs.

### 2f. Tools / ToolConfig / function calling

`Tool` field-name diff:
```
Vertex-only:   urlContext*, retrieval, googleMaps*, computerUse*, googleSearchRetrieval*,
               exaAiSearch, enterpriseWebSearch, googleSearch*, parallelAiSearch, codeExecution*
Dev-API-only:  mcpServers, fileSearch
Shared:        functionDeclarations, codeExecution, googleSearch, googleSearchRetrieval,
               googleMaps, computerUse, urlContext
```
- Vertex-only tool types: `retrieval` (Vertex Search/RAG-style retrieval), `exaAiSearch`, `enterpriseWebSearch`, `parallelAiSearch` — Vertex/enterprise-specific search integrations.
- Developer-API-only: `mcpServers` (native MCP server tool integration), `fileSearch`.
- Core function-calling and built-in-tool surface (`functionDeclarations`, `codeExecution`, `googleSearch`, `googleSearchRetrieval`, `googleMaps`, `computerUse`, `urlContext`) is field-name-identical.

`ToolConfig`: both have `functionCallingConfig` and `retrievalConfig` (identically named/typed). Developer API adds `includeServerSideToolInvocations` (boolean) with no Vertex counterpart.

`FunctionCallingConfig.mode` enum is **fully identical** on both: `MODE_UNSPECIFIED, AUTO, ANY, NONE, VALIDATED`.

`functionCall`/`functionResponse` fields inside `Part` are field-name-identical on both APIs (see §2c).

### 2g. Safety settings / `HarmCategory`

`SafetySetting` shape (`category`, `threshold`) is field-name-identical on both APIs. `HarmCategory` enum size and membership diverge:

- **Vertex** (11 values, from proto/discovery extraction): `HARM_CATEGORY_UNSPECIFIED, HARM_CATEGORY_HATE_SPEECH, HARM_CATEGORY_DANGEROUS_CONTENT, HARM_CATEGORY_HARASSMENT, HARM_CATEGORY_SEXUALLY_EXPLICIT`, plus Vertex-specific/legacy values (`HARM_CATEGORY_IMAGE_HATE`, `HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT`, `HARM_CATEGORY_IMAGE_HARASSMENT`, `HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT`, `HARM_CATEGORY_CIVIC_INTEGRITY`, and others).
- **Developer API** (13 values, direct from Discovery doc): `HARM_CATEGORY_UNSPECIFIED, HARM_CATEGORY_DEROGATORY, HARM_CATEGORY_TOXICITY, HARM_CATEGORY_VIOLENCE, HARM_CATEGORY_SEXUAL, HARM_CATEGORY_MEDICAL, HARM_CATEGORY_DANGEROUS, HARM_CATEGORY_HARASSMENT, HARM_CATEGORY_HATE_SPEECH, HARM_CATEGORY_SEXUALLY_EXPLICIT, HARM_CATEGORY_DANGEROUS_CONTENT, HARM_CATEGORY_CIVIC_INTEGRITY` (deprecated, `enumDeprecated: true`, superseded by `enable_enhanced_civic_answers`), `HARM_CATEGORY_JAILBREAK`. The first six (`DEROGATORY` through `DANGEROUS`) are explicitly labeled legacy "PaLM" values in the schema descriptions.
- `HarmBlockThreshold` enum is **identical** on both: `HARM_BLOCK_THRESHOLD_UNSPECIFIED, BLOCK_LOW_AND_ABOVE, BLOCK_MEDIUM_AND_ABOVE, BLOCK_ONLY_HIGH, BLOCK_NONE, OFF`.
- **Adapter guidance:** the safe common subset across both APIs is `HARM_CATEGORY_HATE_SPEECH`, `HARM_CATEGORY_DANGEROUS_CONTENT`, `HARM_CATEGORY_HARASSMENT`, `HARM_CATEGORY_SEXUALLY_EXPLICIT`. Do not assume 1:1 parity beyond those four.

### 2h. `Candidate.finishReason` — significant divergence

```
Vertex (17 values):   FINISH_REASON_UNSPECIFIED, STOP, MAX_TOKENS, SAFETY, RECITATION, OTHER,
                       BLOCKLIST, PROHIBITED_CONTENT, SPII, MALFORMED_FUNCTION_CALL,
                       MODEL_ARMOR, IMAGE_SAFETY, IMAGE_PROHIBITED_CONTENT, IMAGE_RECITATION,
                       IMAGE_OTHER, UNEXPECTED_TOOL_CALL, NO_IMAGE

Dev API (21 values):  FINISH_REASON_UNSPECIFIED, STOP, MAX_TOKENS, SAFETY, RECITATION, LANGUAGE,
                       OTHER, BLOCKLIST, PROHIBITED_CONTENT, SPII, MALFORMED_FUNCTION_CALL,
                       IMAGE_SAFETY, IMAGE_PROHIBITED_CONTENT, IMAGE_OTHER, NO_IMAGE,
                       IMAGE_RECITATION, UNEXPECTED_TOOL_CALL, TOO_MANY_TOOL_CALLS,
                       MISSING_THOUGHT_SIGNATURE, MALFORMED_RESPONSE, ESCALATION
```
- **Vertex-only:** `MODEL_ARMOR` (ties to the Vertex-only `modelArmorConfig` request field).
- **Developer-API-only:** `LANGUAGE`, `TOO_MANY_TOOL_CALLS`, `MISSING_THOUGHT_SIGNATURE`, `MALFORMED_RESPONSE`, `ESCALATION`.
- **Shared (13 values):** `FINISH_REASON_UNSPECIFIED, STOP, MAX_TOKENS, SAFETY, RECITATION, OTHER, BLOCKLIST, PROHIBITED_CONTENT, SPII, MALFORMED_FUNCTION_CALL, IMAGE_SAFETY, IMAGE_PROHIBITED_CONTENT, IMAGE_OTHER, IMAGE_RECITATION, UNEXPECTED_TOOL_CALL, NO_IMAGE`.
- **Adapter guidance:** any shared internal finish-reason enum must handle unknown-to-one-side values gracefully (not assume a closed, fully-shared set). Vertex will never emit `LANGUAGE`, `TOO_MANY_TOOL_CALLS`, `MISSING_THOUGHT_SIGNATURE`, `MALFORMED_RESPONSE`, or `ESCALATION`; the Developer API will never emit `MODEL_ARMOR`.

### 2i. `Candidate` — other top-level fields

```
Vertex-only:    (none)
Dev-API-only:   groundingAttributions, tokenCount
Shared:         avgLogprobs, citationMetadata, content, finishMessage, finishReason,
                groundingMetadata, index, logprobsResult, safetyRatings, urlContextMetadata
```
- `tokenCount` (Dev-API-only, per-candidate) has **no Vertex equivalent** at the `Candidate` level — Vertex reports token counts only at the response-level `UsageMetadata`, never per-candidate.
- `groundingAttributions` (Dev-API-only) is a legacy/older grounding-citation field, superseded by the shared `groundingMetadata`.

### 2j. `PromptFeedback` / `blockReason`

Both APIs use the field name `promptFeedback` at the response top level and `blockReason` inside it — field naming is identical. However, the **enum sentinel value name differs**: the Developer API's "unset" sentinel is `BLOCK_REASON_UNSPECIFIED`, while Vertex's is `BLOCKED_REASON_UNSPECIFIED` ("BLOCKED_" vs "BLOCK_" prefix) — an easy-to-miss divergence for code that special-cases the unset value by exact string match. Developer API's `blockReason` has 6 values (`BLOCK_REASON_UNSPECIFIED, SAFETY, OTHER, BLOCKLIST, PROHIBITED_CONTENT, IMAGE_SAFETY`); Vertex's has 8 values total. **Recommend a fresh, single-pass side-by-side extraction of both full enum lists before writing normalization code** — the sentinel-name divergence is confirmed, but the full 8-vs-6 comparison was assembled from two separate extraction passes rather than one matched diff.

### 2k. `GenerateContentResponse` — top-level fields

```
Vertex-only:    createTime
Dev-API-only:   modelStatus
Shared:         candidates, modelVersion, promptFeedback, responseId, usageMetadata
```
- `createTime` (Vertex-only): response-generation timestamp, standard GCP resource-metadata style field.
- `modelStatus` (Dev-API-only): a `ModelStatus` object, likely surfacing model deprecation/experimental status; full shape not chased down.

### 2l. `UsageMetadata`

```
Vertex-only:    trafficType
Dev-API-only:   serviceTier
Shared:         cacheTokensDetails, cachedContentTokenCount, candidatesTokenCount,
                candidatesTokensDetails, promptTokenCount, promptTokensDetails,
                thoughtsTokenCount, toolUsePromptTokenCount, toolUsePromptTokensDetails,
                totalTokenCount
```
- All core token-count field names are **identical** between the two APIs (`promptTokenCount`, `candidatesTokenCount`, `totalTokenCount`, `cachedContentTokenCount`, `thoughtsTokenCount`, `toolUsePromptTokenCount`, plus the modality-breakdown arrays). The same field-name mapping can be reused for usage-metering code on both providers.
- `trafficType` (Vertex-only enum: `ON_DEMAND, ON_DEMAND_PRIORITY, ON_DEMAND_FLEX, ON_DEMAND_OFFPEAK, PROVISIONED_THROUGHPUT`) — Vertex's billing/quota class-of-service indicator.
- `serviceTier` (Dev-API-only on the response side, mirrors the request-side field from §2b) — reports which tier actually served the request.

### 2m. Consolidated list of Vertex-only fields

- Request body: `labels`, `modelArmorConfig`
- `GenerationConfig`: `routingConfig`, `audioTimestamp`
- `GenerateContentResponse`: `createTime`
- `UsageMetadata`: `trafficType`
- `Candidate.finishReason` enum value: `MODEL_ARMOR`
- `Tool`: `retrieval`, `exaAiSearch`, `enterpriseWebSearch`, `parallelAiSearch`
- Model addressing: fully-qualified `projects/{project}/locations/{location}/publishers/google/models/{model}` URL-path-only addressing (no body-level `model` field), vs. Developer API's bare `models/{model}` (also accepted as an optional/redundant body field)

VPC Service Controls and CMEK/encryption fields were **not found** on the `generateContent`/`streamGenerateContent` request or response schemas — these are enforced at the project/network-perimeter level (VPC-SC boundary, CMEK on underlying storage), not as JSON fields on this RPC. **UNCERTAIN / likely not adapter-code-visible** — flag as an org-level Vertex control orthogonal to the request schema.

**Sources for §2 (all subsections):** live diff of `https://aiplatform.googleapis.com/$discovery/rest?version=v1` and `https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta`; cross-checked against `google/cloud/aiplatform/v1/content.proto` and `google/ai/generativelanguage/v1beta/generative_service.proto` in `googleapis/googleapis`.

---

## 3. Authentication

**Confirmed:** standard (non-Express) Vertex AI REST calls require an `Authorization: Bearer <access_token>` header, where the token is obtained through the normal Application Default Credentials (ADC) flow (`gcloud auth print-access-token` in curl/PowerShell examples in Google's docs, or `google.auth.default()` + `credentials.refresh()` in Python).
Source: `https://docs.cloud.google.com/vertex-ai/docs/authentication`. This page also states ADC impersonation is "supported only for Go, Java, Node.js, and Python client libraries" — Python is explicitly covered.

**OAuth scope confirmed:** `https://www.googleapis.com/auth/cloud-platform`. Verified against the live Google API Discovery Document itself (`https://aiplatform.googleapis.com/$discovery/rest?version=v1`), whose `auth.oauth2.scopes` block lists exactly two scopes for the entire `aiplatform.googleapis.com` surface: `https://www.googleapis.com/auth/cloud-platform` ("See, edit, configure, and delete your Google Cloud data and see the email address for your Google Account") and `https://www.googleapis.com/auth/cloud-platform.read-only`. This is machine-generated directly by the API infrastructure, not docs prose that could be stale — the strongest possible citation for "what scope is required." Corroborated by Google's own Vertex-AI-Gemini-as-OpenAI-endpoint migration guide, which uses this exact scope in its reference code:
```python
credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
credentials.refresh(google.auth.transport.requests.Request())
```
Source: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/migrate/openai/auth-and-credentials`.

**Confirmed fundamentally different from the Developer API's `x-goog-api-key` approach.** The primary/production Vertex AI path does not accept a simple API key for `generateContent`.

**One caveat worth noting (not the primary path for this gateway):** "Vertex AI Express Mode" exists in 2026 under the "Gemini Enterprise Agent Platform" rebrand (see §6) and uses an API key passed as a `?key=` query parameter on a simplified endpoint (no `projects/{project}/locations/{location}` path segments) — not an `x-goog-api-key` header, and not the standard OAuth2/Bearer flow. It is a 90-day trial/quota-limited product, administratively and architecturally separate from the production ADC/OAuth2 path this gateway needs.
Source: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/express-mode/overview`.

---

## 4. `google-auth` Python library fit for an async httpx service using impersonated-SA ADC JSON

### Package identity and version

PyPI package name is unchanged: `google-auth`. Current version, confirmed via direct PyPI JSON API query: **2.56.3**, released **2026-08-06**, `requires_python: >=3.10`.
Source: `https://pypi.org/pypi/google-auth/json`.

The source repo was archived from `googleapis/google-auth-library-python` on 2026-03-06 and now lives at `googleapis/google-cloud-python` (a monorepo move under `packages/google-auth`); the PyPI package name and `google.auth` import path are unaffected.
Source: repository state observed directly during this research.

### Loading impersonated-SA ADC JSON — automatic, no manual construction needed

Confirmed by reading `google/auth/_default.py` in the current monorepo source directly: the constant `_IMPERSONATED_SERVICE_ACCOUNT_TYPE = "impersonated_service_account"` is matched inside `_load_credentials_from_info()`, which dispatches to `_get_impersonated_service_account_credentials()`, which calls `impersonated_credentials.Credentials.from_impersonated_service_account_info(info, scopes=scopes)`.

This means a plain call of
```python
credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
```
(or `google.auth.load_credentials_from_file(path, scopes=[...])` pointed at the ADC JSON produced by `gcloud auth application-default login --impersonate-service-account=...`) transparently returns a working `impersonated_credentials.Credentials` instance. **No manual `impersonated_credentials.Credentials(source_credentials=..., target_principal=..., ...)` construction is required** for this credential type.
Source: `google/auth/_default.py`, `googleapis/google-cloud-python` (direct source read).

### `Credentials.refresh()` is synchronous/blocking — confirmed, including in Google's own reference implementation

Google's own OpenAI-compatibility-layer migration doc ships this exact wrapper pattern for exposing Vertex-backed credentials:
```python
self.creds, self.project = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
...
if not self.creds.valid:
    self.creds.refresh(google.auth.transport.requests.Request())
    if not self.creds.valid:
        raise RuntimeError("Unable to refresh auth")
```
Source: `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/migrate/openai/auth-and-credentials`.

This confirms three things simultaneously: (1) `google.auth.transport.requests.Request()` — the synchronous, `requests`-library-backed transport — is the canonical/only transport Google itself uses for this; (2) `Credentials` exposes a `.valid` boolean property meant to gate refresh calls; (3) even Google's current official example is blocking code with no async variant offered.

### `google.auth.aio` exists but does not support impersonated credentials

`google.auth.aio` ships inside the same `google-auth` distribution (not a separately versioned/installed package — its `__version__` is imported from the parent package). However:

- The async ADC loader, `google/auth/_default_async.py`, only recognizes `_default._AUTHORIZED_USER_TYPE` and `_default._SERVICE_ACCOUNT_TYPE` in its `load_credentials_from_file()`. Any other `"type"` value — including `impersonated_service_account` — falls through and raises `google.auth.exceptions.DefaultCredentialsError`. Confirmed by direct source read.
- The only concrete async `Credentials` subclass shipped in `google/auth/aio/credentials.py` is `StaticCredentials` (an immutable, pre-fetched token holder with no refresh logic). There is no async `ImpersonatedCredentials` class anywhere in the codebase.
- **UNCERTAIN**: the exact version `google.auth.aio` was first introduced could not be pinned (no changelog entry found co-occurring with "aio"). This is immaterial to the design question, since the async path does not support the required credential type at any version.

Source: direct read of `googleapis/google-cloud-python`, `packages/google-auth/google/auth/_default_async.py` and `google/auth/aio/credentials.py`.

### Recommended pattern: offload the blocking refresh

Since there is no viable async-native path for this credential type, the standard approach is to wrap `credentials.refresh(Request())` in `asyncio.to_thread(...)` (or `loop.run_in_executor(None, ...)`) so it does not block the FastAPI/httpx event loop. No Google blog post or doc was found that explicitly prescribes this exact pairing for `google-auth` — this is standard, widely-used asyncio guidance for wrapping blocking I/O, not a documented Google recommendation. **Flagged as UNCERTAIN as a Google-sourced recommendation**, though it is the de facto community pattern for sync-`google-auth`-plus-async-web-framework.

### Token lifetime and refresh margin

- Google's authoritative lifetime table (`https://docs.cloud.google.com/docs/authentication/token-types`, directly fetched): "User access token" = 1 hour (fixed); **"Service account access token" = 5 minutes–12 hours, defaulting to 1 hour**, extendable only via the IAM `serviceAccounts.generateAccessToken` method and only if the org policy constraint `iam.allowServiceAccountCredentialLifetimeExtension` permits it.
- `google/auth/impersonated_credentials.py` hard-codes `_DEFAULT_TOKEN_LIFETIME_SECS = 3600` (1 hour) as the `lifetime` constructor default ("Number of seconds the delegated credential should be valid for (upto 3600)"). Reconciles with the table above: plan on ~1-hour token lifetimes by default unless `lifetime` is explicitly overridden (and org policy allows extension).
- **A real-world gotcha worth carrying into the spec**: a live bug report (`https://github.com/pydantic/pydantic-ai/issues/1186`) shows Google-issued access tokens do **not** always live the full 3600 seconds — that report observed 1800s for a short-lived impersonation token. An adapter must not hardcode a refresh-margin assumption based on issuance time; it should read the credential object's own `expiry`/`valid` state rather than track issuance time itself.
- **Refresh margin is precisely confirmed**: `google/auth/_helpers.py` defines `REFRESH_THRESHOLD = datetime.timedelta(minutes=3, seconds=45)`; `Credentials.expired` (in `google/auth/credentials.py`) computes `skewed_expiry = self.expiry - REFRESH_THRESHOLD` and returns `True` once `utcnow() >= skewed_expiry`. **`google-auth`'s own `.expired`/`.valid` properties already build in a ~3m45s refresh buffer** — the adapter does not need to implement its own clock-skew margin. Checking `credentials.valid` before each outbound Vertex call (as in Google's own OpenAI-compat example above) is sufficient and idiomatic.

Sources: `https://docs.cloud.google.com/docs/authentication/token-types`; direct source read of `google/auth/impersonated_credentials.py`, `google/auth/_helpers.py`, `google/auth/credentials.py` in `googleapis/google-cloud-python`.

### Alternative async transport packages

Resolved via direct PyPI JSON API queries (not WebSearch, to avoid stale cached version numbers):

- **`gcloud-aio-auth`** — `https://pypi.org/project/gcloud-aio-auth/` — current version **5.5.0**, released **2026-07-17**. Actively maintained. Provides a native-async, `aiohttp`-backed `Token` credentials class. **UNCERTAIN**: whether its `Token` class has confirmed support for `impersonated_service_account`-type ADC JSON specifically was not re-verified against its source — flag for a follow-up source check before relying on it.
- **`aiogoogle`** — `https://pypi.org/project/aiogoogle/` — current version **5.19.0**, released **2026-07-18**. Actively maintained. A full discovery-document-driven async Google API client, not a narrow auth shim — heavier than needed if the sole requirement is async-safe token refresh for hand-rolled httpx calls.
- **`google-auth-httpx`** — confirmed to **not exist** on PyPI: both `https://pypi.org/pypi/google-auth-httpx/json` and `https://pypi.org/simple/google-auth-httpx/` return HTTP 404 as of 2026-08-07.

Neither `gcloud-aio-auth` nor `aiogoogle` has confirmed, documented support for the specific `impersonated_service_account` ADC JSON credential type. (Note: this report does not make an architecture recommendation — that decision belongs to the calling process. The above is reported as fact-finding only, per the research brief's explicit instruction not to recommend architecture; a "bottom-line" synthesis was offered by the research sub-agent but is presented here as input, not a decision.)

---

## 5. Error modes specific to Vertex AI auth/config

### Baseline error envelope

All Vertex AI / Google Cloud API errors use the standard `google.rpc.Status` envelope (AIP-193, `https://google.aip.dev/193` — this is the current redirect target of the older `cloud.google.com/apis/design/errors` URL). Top level: `{"error": {"code": <int>, "message": <string>, "status": <UPPER_SNAKE gRPC status name>, "details": [...]}}`. HTTP-to-status mapping: 401 → `UNAUTHENTICATED`, 403 → `PERMISSION_DENIED`, 404 → `NOT_FOUND`, 429 → `RESOURCE_EXHAUSTED`.

Most `details[]` entries use `ErrorInfo`: `{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": <UPPER_SNAKE, max 63 chars>, "domain": <string>, "metadata": {...}}`. The `reason` field is the machine-readable code an adapter should match on — the free-text `message` can change wording without notice.

### 5.1 Expired or invalid OAuth2 bearer token (401)

Confirmed real body from a live bug report (`https://github.com/pydantic/pydantic-ai/issues/1186`):
```json
{
  "error": {
    "code": 401,
    "message": "Request had invalid authentication credentials. Expected OAuth 2 access token, login cookie or other valid authentication credential. See https://developers.google.com/identity/sign-in/web/devconsole-project.",
    "status": "UNAUTHENTICATED",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "ACCESS_TOKEN_EXPIRED",
        "domain": "googleapis.com",
        "metadata": {
          "service": "aiplatform.googleapis.com",
          "method": "google.cloud.aiplatform.v1.PredictionService.GenerateContent"
        }
      }
    ]
  }
}
```
`code=401`, `status=UNAUTHENTICATED`, `reason=ACCESS_TOKEN_EXPIRED`; `metadata.service`/`metadata.method` identify exactly which upstream call failed — useful for adapter-side error classification.

### 5.2 Wrong/misconfigured GCP project ID or location

Two distinct failure shapes:

**Wrong/nonexistent project** — confirmed real body (`https://github.com/GoogleCloudPlatform/training-data-analyst/issues/1811`): a **403**, not 404, with `reason: CONSUMER_INVALID`. Google treats an unrecognized/unauthorized project as a permission failure on the caller's project ("consumer"), not a not-found. Adapters should not assume "bad project" always surfaces as 404.

**ADC-level project confusion** — Google's ADC troubleshooting page (`https://docs.cloud.google.com/docs/authentication/troubleshoot-adc`, directly fetched) documents a related trap: when using gcloud user credentials or a misconfigured ADC file, the SDK can silently resolve to the placeholder project `764086051850` ("Unknown project 764086051850") because that is a fixed ID gcloud's own OAuth client uses internally. This manifests as confusing downstream 403/404s. It is specific to gcloud user ADC rather than service-account impersonation, so likely does not apply if the gateway always uses SA impersonation — noted as a diagnostic aid. The same page also documents "quota project" mismatches (a different project gets billed/quota-checked) as a separate config-error class.

**Invalid/unsupported location** — no directly-quoted invalid-location error body was obtained (the authoritative locations page, `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations`, returned only navigation content on repeated fetch attempts). Based on general Vertex AI API behavior (search-snippet corroboration, not a direct quote), an unsupported/misspelled `location` in the URL path likely surfaces as a **404 (`NOT_FOUND`)**, since the regional hostname resolves but the `.../locations/{location}/...` resource path does not exist for that project. **UNCERTAIN** — recommend empirical verification against a real misspelled-region call before relying on this in retry/error-classification logic.

### 5.3 Missing IAM role/permission — two independent failure points

This is the most important distinction for the spec, since impersonation-based auth has **two independent IAM checks** that fail at different times and in different ways.

**(a) Impersonation itself denied — fails at the IAM Credentials API, before Vertex AI is ever called.** When the gateway's base credential lacks `roles/iam.serviceAccountTokenCreator` (or the narrower `iam.serviceAccounts.getAccessToken` permission) on the target service account, the failure happens during token minting — a call to `iamcredentials.googleapis.com`'s `generateAccessToken` method (`https://docs.cloud.google.com/iam/credentials/reference/rest/v1/projects.serviceAccounts/generateAccessToken`) — and Vertex AI is never reached. Real-world report and shape (`https://discuss.google.dev/t/error-code-403-permission-iam-serviceaccounts-getaccesstoken-denied-on-resource/147169`):
```json
{
  "error": {
    "code": 403,
    "message": "Permission 'iam.serviceAccounts.getAccessToken' denied on resource (or it may not exist).",
    "status": "PERMISSION_DENIED",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "IAM_PERMISSION_DENIED",
        "domain": "iam.googleapis.com",
        "metadata": { "permission": "iam.serviceAccounts.getAccessToken" }
      }
    ]
  }
}
```
**Medium confidence** on the exact field values (sourced from a secondary community-forum summary rather than a direct primary-doc quote); the overall envelope shape (403/`PERMISSION_DENIED`/`ErrorInfo` with `domain: iam.googleapis.com`) is consistent with AIP-193. Operationally notable: the same source reports IAM role-grant propagation delay of "usually about two minutes, but sometimes seven or more minutes" — an adapter should not treat this 403 as instantly-permanent immediately after a role grant.

**(b) Caller (the impersonated SA) reaches Vertex AI but lacks `roles/aiplatform.user`.** A distinct, later-stage 403 — the token is valid and project/location resolve, but the SA is not authorized to call `generateContent`. Google's IAM permission-error-messages doc (`https://docs.cloud.google.com/iam/docs/permission-error-messages`, directly fetched) confirms the general 403/`PERMISSION_DENIED`/`ErrorInfo` shape for this class of error. For Vertex AI specifically, community reports (`https://discuss.google.dev/t/permission-and-role-issue-with-vertex-ai/183672`) point to the relevant permission as `aiplatform.endpoints.predict` (or the `generateContent`-equivalent permission bundled into `roles/aiplatform.user`). **UNCERTAIN**: no directly-quoted, primary-source raw JSON body specific to this exact scenario was obtained (the official access-control doc, `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/access-control`, returned only nav content); the `reason` string is inferred by analogy to §5.3a as likely `IAM_PERMISSION_DENIED`, not directly confirmed for this specific scenario.

**Practical implication:** the adapter's 403-handling should distinguish these two cases by which service raised the error (`iamcredentials.googleapis.com` vs `aiplatform.googleapis.com`, visible in `metadata.service` or in which internal HTTP call raised it) — a 403 during token minting means "fix the impersonation grant"; a 403 during `generateContent` means "grant `roles/aiplatform.user`." These require different remediation/alerting paths.

### 5.4 Model not available in the requested region

Confirmed via a real forum thread (`https://discuss.google.dev/t/vertex-ai-generatecontent-fails-with-404-publisher-model-not-found/188358`): the exact message text is **"404 Publisher Model ... was not found or your project does not have access to it,"** `status: NOT_FOUND`. This is deliberately ambiguous by design — Google folds "model doesn't exist," "model isn't available in this region," and "project lacks access" into the same generic 404. **The adapter cannot reliably distinguish these causes purely from the error body** (no `ErrorInfo.reason` breakout was found for this case); region-availability validation should ideally happen client-side against a known model/region support table rather than by parsing this message.

### 5.5 Quota exceeded (429)

**Important finding, revises the naive AIP-193/`QuotaFailure` expectation:** real observed Vertex AI 429 bodies do **not** use the `google.rpc.QuotaFailure` proto (with `violations[].subject`/`violations[].description`/quota-metric fields). Instead, a real, directly-fetched Vertex AI 429 body (`https://github.com/google-gemini/gemini-cli/issues/11958`) uses the **older, legacy Google API `errors[]` array shape** (predates the AIP-193/`ErrorInfo` convention):
```json
{
  "error": {
    "code": 429,
    "message": "...",
    "errors": [{ "message": "...", "domain": "global", "reason": "rateLimitExceeded" }],
    "status": "RESOURCE_EXHAUSTED"
  }
}
```
`code=429`, `status=RESOURCE_EXHAUSTED`, plain `reason: "rateLimitExceeded"` inside a legacy `errors[]` array — not `ErrorInfo`, not `QuotaFailure.violations`. Quota-metric-level detail (which specific quota was hit) is **not confirmed present** in real Vertex 429 responses. **UNCERTAIN**: treat any expectation of a structured "which quota metric exceeded" field in the runtime error body as unverified; design 429 handling around the coarser `reason: rateLimitExceeded` signal plus standard exponential-backoff retry, per Google's own mitigation guidance (`https://cloud.google.com/blog/products/ai-machine-learning/reduce-429-errors-on-vertex-ai`, directly fetched — recommends Provisioned Throughput / Flex PayGo consumption-model changes and retry/backoff, not client-side quota introspection).

The named quota metric `aiplatform.googleapis.com/generate_content_requests_per_minute_per_project_per_base_model` is real and documented (multiple Google Developer Forum threads reference it by exact name, e.g. `https://discuss.google.dev/t/quota-exceeded-error-for-generate-content-requests-per-minute-per-project-per-base-model-per-minute/164722`) but appears in the **Cloud Console quota-management UI**, not confirmed inside the runtime 429 error body itself.

**Quota model shift for newer models:** models before Gemini 2.0 use fixed per-project-per-region quotas requiring manual increase requests; Gemini 2.0+ models use "Dynamic Shared Quota" (DSQ), pooling PayGo capacity across customers per model/region and removing the fixed-limit/increase-request model. **Medium confidence** — corroborated by multiple independent search snippets referencing `https://cloud.google.com/vertex-ai/generative-ai/docs/quotas`, but a direct verbatim quote of that page could not be obtained (returned only nav content on repeated fetch attempts).

**Comparison to the Gemini Developer API's 429:** the Developer API rate-limits doc (`https://ai.google.dev/gemini-api/docs/rate-limits`, directly fetched) confirms the Developer API also returns `429 RESOURCE_EXHAUSTED` but does not publish an example JSON body on that page (confirmed absence — the page explicitly points to `https://ai.google.dev/gemini-api/docs/api-errors` for the schema instead, not independently fetched in this pass). A real Developer API 429 example found separately (`https://github.com/google-gemini/deprecated-generative-ai-python/issues/244`) shows quota metric name `'Generate Content API requests per minute'` with `reason: "RATE_LIMIT_EXCEEDED"` (different capitalization/wording from Vertex's `rateLimitExceeded`) against consumer `generativelanguage.googleapis.com`. Architecturally: Developer API rate limits are dimensioned as RPM/RPD/TPM/IPM **per Google Cloud project** (not per API key), with daily limits resetting at midnight Pacific time; Vertex AI's model is project+region+model based and, for newer models, pooled via DSQ rather than a fixed daily/minute allowance. **UNCERTAIN**: whether the Developer API's `reason` string is consistently `RATE_LIMIT_EXCEEDED` across all 429 causes, or specific to the one example found, was not confirmed.

**Caveat on §5 overall:** two of the requested primary-source pages (Vertex locations/model-availability doc; quotas/error-code-429 doc) could not be content-extracted via direct fetch across multiple attempts each (navigation-chrome only, despite 200 OK responses). Findings dependent on those pages were triangulated via WebSearch snippets and real, pasted error bodies from GitHub issues and Google's developer forums — independently verifiable and generally reliable, but recommend a direct browser check of those two pages before finalizing behavior that depends on them (region-availability error semantics; exact structure/absence of `QuotaFailure` in 429 bodies).

---

## 6. Other 2026-current gotchas

### SDK unification — confirmed, with a 2026-specific naming update

Google has unified the Gemini Developer API and Vertex AI under one SDK: `google-genai` (PyPI: `https://pypi.org/project/google-genai/`, current version **2.17.0, released 2026-08-06**, confirmed via direct PyPI fetch). GitHub: `https://github.com/googleapis/python-genai`.

Direct source read of `Client.__init__` (`https://raw.githubusercontent.com/googleapis/python-genai/main/google/genai/client.py`) shows the constructor now has **two** relevant boolean parameters: `vertexai` (in-source comment: "Legacy flag for enterprise") and a newer, preferred `enterprise` parameter — both route to the same backend; the client raises `ValueError` on conflicting values. The README (`https://raw.githubusercontent.com/googleapis/python-genai/main/README.md`, directly fetched) confirms the current-preferred pattern is `Client(enterprise=True, ...)` with equivalent env var `GOOGLE_GENAI_USE_ENTERPRISE`; `GOOGLE_GENAI_USE_VERTEXAI` was not found in the current README (likely still functional via the legacy `vertexai` param but no longer the documented-first path). This naming directly reflects the rebrand below — most existing tutorials/community answers will only show `vertexai=True`.

**Major 2026 rebrand, high confidence (primary-source direct fetch):** Google has rebranded "Vertex AI" generative-AI/agent product surfaces to **"Gemini Enterprise Agent Platform,"** announced around Google Cloud Next 2026 (~late April 2026). The official Vertex AI Gemini release-notes page (`https://docs.cloud.google.com/vertex-ai/generative-ai/docs/release-notes`, directly fetched) explicitly documents this rebrand and states the Vertex AI generative-AI docs were **frozen as of May 26, 2026** (new updates go to the renamed docs surface instead). Corroborated by many URLs now resolving under `docs.cloud.google.com/gemini-enterprise-agent-platform/...` in place of former `.../vertex-ai/generative-ai/...` paths (e.g. an "Error code 429" page seen at both `.../vertex-ai/generative-ai/docs/provisioned-throughput/error-code-429` and `.../gemini-enterprise-agent-platform/models/deploy/error-code-429`).

**Important for the spec: underlying hostnames, proto namespaces, and model resource IDs are NOT changed by this rebrand.** Confirmed by the release-notes page and by every real error body collected in this research still showing `service: aiplatform.googleapis.com`. This is a product/documentation branding change, not a backend/API change — the wire-level design (hostnames, paths, proto shapes documented in §§1–2) is unaffected; only doc URLs and marketing names shift.

**Related, easy to miss:** essentially every `cloud.google.com/vertex-ai/...` doc URL now 301-redirects to `docs.cloud.google.com/vertex-ai/...` — confirmed empirically across every fetch performed in this research. Old links still resolve via redirect, but new citations should prefer the `docs.cloud.google.com` domain directly.

### Legacy SDK deprecation — confirmed with exact dates

The older `google-cloud-aiplatform` SDK's generative-AI submodules (`vertexai.generative_models`, `vertexai.language_models`, `vertexai.vision_models`, `vertexai.tuning`, `vertexai.caching`) were deprecated **2025-06-24**, and per Google's own deprecation page (`https://docs.cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk`, directly fetched), SDK releases published after **2026-06-24** no longer include these modules at all. Since that cutoff has already passed as of today (2026-08-07), any `google-cloud-aiplatform` version released in roughly the last six weeks does not contain `vertexai.generative_models.GenerativeModel` — a new adapter should not be built against that legacy class; it is being actively removed from current releases, not merely discouraged.

### Model naming / current generation

Current-generation model families as of this research (August 2026): Gemini 3 / Gemini 3.1 (Pro, Flash, Flash-Lite) are current alongside the still-supported Gemini 2.5 family (Pro, Flash, Flash Image) and Gemini 2.0. **UNCERTAIN**: an earlier research pass surfaced an October 16, 2026 retirement date for Gemini 2.5 Pro/Flash/Flash-Lite, but this could not be re-verified against a primary source in this pass — recommend the release-notes page (`https://docs.cloud.google.com/vertex-ai/generative-ai/docs/release-notes`) be checked again at spec-finalization time, since retirement dates shift.

### API versioning recap

See §1b — `v1` is GA and has full parity with `v1beta1` on the `generateContent` surface as verified; the unified SDK defaults to `v1beta` unless `v1` is explicitly requested via `http_options`. No primary-source recommendation of one REST version over the other specifically for hand-rolled callers was found (**UNCERTAIN**).

### Endpoint/model-availability recap

See §1c for the global endpoint (GA ~2025-05-02, purpose is 429 reduction, path retains `locations/global`, hostname drops the region prefix entirely).

---

## Consolidated list of UNCERTAIN items (for spec-writer attention)

1. Exact semantics of the `aiplatform.us.rep.googleapis.com` / `aiplatform.eu.rep.googleapis.com` "Regional Endpoint" hosts seen in the Discovery document (§1a).
2. Whether any fields/capabilities are v1beta1-exclusive on Vertex, and whether Google has a primary-source recommendation of v1 vs v1beta1 for direct REST callers (§1b, §6).
3. Exact global-endpoint URL path string, corroborated but not confirmed via a second verbatim primary-source quote (§1c).
4. Complete, itemized list of which Gemini models support the global endpoint (§1c).
5. `alt=sse` streaming framing behavior — high confidence via convergent third-party evidence, but no single canonical Google doc quote obtained; recommend a live smoke test (§1d).
6. Purpose/shape of the Developer-API-only `store` request field (§2b) and `modelStatus` response field (§2k).
7. Full side-by-side `blockReason` enum diff (8 Vertex values vs 6 Developer API values) — sentinel-name divergence (`BLOCKED_REASON_UNSPECIFIED` vs `BLOCK_REASON_UNSPECIFIED`) is confirmed, but a fresh matched extraction of every other value was not performed in one pass (§2j).
8. Whether VPC-SC/CMEK have any adapter-visible request/response fields on `generateContent` (likely not — org-level controls) (§2m).
9. Whether `gcloud-aio-auth`'s `Token` class supports `impersonated_service_account`-type ADC JSON (§4).
10. Exact error `reason` string(s) for a 403 caused by a missing `roles/aiplatform.user`-equivalent grant when calling `generateContent` itself (as opposed to the impersonation-minting 403) — inferred by analogy, not directly confirmed (§5.3b).
11. Exact error shape/status for an invalid/misspelled `location` path segment (§5.2).
12. Whether Vertex 429 bodies ever carry `QuotaFailure`-style structured quota-metric detail, versus only the plain `reason: rateLimitExceeded` string observed (§5.5).
13. Precise Dynamic Shared Quota (DSQ) mechanics — corroborated by search snippets only, no verbatim primary-source quote (§5.5).
14. Exact Gemini 2.5 family retirement date (an unverified October 16, 2026 figure surfaced but was not re-confirmed) (§6).

---

## Full source list

- `https://aiplatform.googleapis.com/$discovery/rest?version=v1` (Vertex AI REST Discovery document, v1, revision 20260801)
- `https://aiplatform.googleapis.com/$discovery/rest?version=v1beta1` (Vertex AI REST Discovery document, v1beta1, revision 20260801)
- `https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta` (Gemini Developer API REST Discovery document, revision 20260806)
- `googleapis/googleapis` GitHub repo: `google/cloud/aiplatform/v1/content.proto`, `google/cloud/aiplatform/v1/prediction_service.proto`, `google/ai/generativelanguage/v1beta/generative_service.proto`
- `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1/projects.locations.publishers.models/generateContent`
- `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations`
- `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/release-notes`
- `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk`
- `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/migrate/openai/auth-and-credentials`
- `https://docs.cloud.google.com/vertex-ai/docs/authentication`
- `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/express-mode/overview`
- `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/access-control`
- `https://docs.cloud.google.com/vertex-ai/generative-ai/docs/quotas`
- `https://docs.cloud.google.com/docs/authentication/token-types`
- `https://docs.cloud.google.com/docs/authentication/troubleshoot-adc`
- `https://docs.cloud.google.com/iam/docs/permission-error-messages`
- `https://docs.cloud.google.com/iam/credentials/reference/rest/v1/projects.serviceAccounts/generateAccessToken`
- `https://ai.google.dev/api/generate-content`
- `https://ai.google.dev/gemini-api/docs/rate-limits`
- `https://google.aip.dev/193` (AIP-193 error model)
- `https://cloud.google.com/blog/products/ai-machine-learning/reduce-429-errors-on-vertex-ai`
- `https://pypi.org/pypi/google-auth/json`
- `https://pypi.org/project/google-genai/`
- `https://github.com/googleapis/python-genai` (`client.py`, `README.md`)
- `https://github.com/googleapis/google-cloud-python`, `packages/google-auth/google/auth/_default.py`, `_default_async.py`, `aio/credentials.py`, `impersonated_credentials.py`, `_helpers.py`, `credentials.py`
- `https://pypi.org/project/gcloud-aio-auth/`
- `https://pypi.org/project/aiogoogle/`
- `https://pypi.org/pypi/google-auth-httpx/json` (confirmed non-existent)
- Real-world error-body evidence: `https://github.com/pydantic/pydantic-ai/issues/1186`, `https://github.com/GoogleCloudPlatform/training-data-analyst/issues/1811`, `https://discuss.google.dev/t/error-code-403-permission-iam-serviceaccounts-getaccesstoken-denied-on-resource/147169`, `https://discuss.google.dev/t/permission-and-role-issue-with-vertex-ai/183672`, `https://discuss.google.dev/t/vertex-ai-generatecontent-fails-with-404-publisher-model-not-found/188358`, `https://github.com/google-gemini/gemini-cli/issues/11958`, `https://discuss.google.dev/t/quota-exceeded-error-for-generate-content-requests-per-minute-per-project-per-base-model-per-minute/164722`, `https://github.com/google-gemini/deprecated-generative-ai-python/issues/244`
- Streaming-behavior corroboration: `https://github.com/BerriAI/litellm/issues/15293`, `https://github.com/BerriAI/litellm/issues/27444`, `https://github.com/musistudio/claude-code-router/issues/1315`, `https://github.com/cline/cline/issues/918`
