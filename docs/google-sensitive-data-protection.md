# Google Sensitive Data Protection PII backend

Status: accepted and implemented for `ai-gateway-ohg`.

## Decision

Use Google Cloud Sensitive Data Protection's synchronous `inspectContent` API as the production PII detector. Keep Presidio only as an explicitly selected local/migration rollback backend until the Google path completes its live soak.

The product is named Sensitive Data Protection, but the API remains the Cloud Data Loss Prevention API (`dlp.googleapis.com`) and the Python package remains `google-cloud-dlp`.

## Why replace Presidio

| Concern | Google Sensitive Data Protection | Presidio/spaCy |
|---|---|---|
| Detection quality | Managed infoType detectors with likelihood filtering; evaluated against the gateway corpus | Local NER produced destructive proper-noun false positives such as `Django -> PERSON` |
| Latency | Network call on every inspected request; bounded by a five-second default timeout and retry deadline | Local CPU inference; no network hop |
| Availability | Depends on Google API, ADC, quota, and network; gateway fails closed | Runs locally but competes for process CPU/thread capacity |
| Privacy/residency | Raw request text is sent to the configured Google processing location; regional endpoint is available where supported | Raw text remains inside Governance |
| Cost/quota | Billable bytes inspected and API quotas must be monitored | Compute/model-image cost only |
| Operations | Managed detectors; IAM, API enablement, quota, billing, and regional routing required | Model downloads, recognizer tuning, upgrades, and concurrency management required |

The managed backend wins for production accuracy and maintenance. Presidio remains useful as a no-cloud local fallback, not as an automatic fail-open fallback. Google errors never silently switch to Presidio.

## Runtime contract

Set `PII_BACKEND=google`. Governance then:

1. creates `google.cloud.dlp_v2.DlpServiceClient` using Application Default Credentials;
2. calls `inspectContent` with `include_quote=false`, an explicit infoType allowlist, minimum likelihood, finding limit, retry deadline, and RPC timeout;
3. converts Google code-point ranges to the gateway's character-offset contract (with UTF-8 byte-range fallback);
4. maps `PERSON_NAME` to `PERSON` and `US_SOCIAL_SECURITY_NUMBER` to `US_SSN` for compatibility;
5. replaces findings locally with visible typed markers such as `[EMAIL_ADDRESS]`; and
6. returns the existing `PiiResult` shape without matched text.

Redaction is local rather than a second `deidentifyContent` call. This halves billable/network work, preserves exact gateway marker syntax, and avoids sending the same raw prompt twice. Inputs larger than one synchronous request are split into overlapping UTF-8-safe chunks below Google's 0.5 MB request limit; overlap findings are deduplicated using global character offsets. Truncated provider results fail closed rather than leaking unmatched values. Google likelihood values are ordinal categories, not calibrated probabilities; the gateway maps them to stable compatibility scores from `0.1` through `0.9`.

Any authentication, quota, timeout, transport, malformed-range, or provider error raises a sanitized `PiiBackendError`. The governance request fails, so proxy and MCP callers retain their existing fail-closed behavior. Provider diagnostics and prompt text are not included in the public error.

## Configuration

| Variable | Required | Meaning |
|---|---:|---|
| `PII_BACKEND` | yes | `google` for production; `presidio` only for explicit rollback/local use |
| `GOOGLE_CLOUD_PROJECT` | with Google | Project billed for and authorizing DLP calls |
| `GOOGLE_DLP_LOCATION` | no | Processing location in the request parent; default `global` |
| `GOOGLE_DLP_API_ENDPOINT` | no | Regional endpoint hostname when in-transit residency requires one |
| `GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT` | local impersonation | Effective service-account identity required by local preflight |
| `GOOGLE_DLP_MIN_LIKELIHOOD` | no | `VERY_UNLIKELY` through `VERY_LIKELY`; default `POSSIBLE` |
| `GOOGLE_DLP_TIMEOUT_SECONDS` | no | Per-call timeout/retry deadline, 0-30 seconds; default 5 |
| `GOOGLE_DLP_INFO_TYPES` | no | Comma-separated detector allowlist |
| `GOOGLE_APPLICATION_CREDENTIALS_HOST` | local Compose only | Host impersonated-ADC path mounted read-only into Governance |
| `GOOGLE_ADC_STATUS_PATH` | local Compose override | Metadata-only credential-sentinel status consumed by Governance readiness |
| `GOOGLE_ADC_STATUS_MAX_AGE_SECONDS` | no | Maximum sentinel age before fail-closed; default 90 seconds |
| `GOOGLE_ADC_RETRY_AFTER_SECONDS` | no | `Retry-After` returned while DLP credentials are unavailable; default 60 seconds |

The default detector set is:

`EMAIL_ADDRESS,PHONE_NUMBER,US_SOCIAL_SECURITY_NUMBER,CREDIT_CARD_NUMBER,IP_ADDRESS,STREET_ADDRESS,DATE_OF_BIRTH`

`PERSON_NAME` is intentionally not enabled by default. Live evaluation showed that
Google correctly left `Django` alone but still classified `Gemini` as a possible
person and `Claude` as a likely person. A bare name is too ambiguous to redact
safely in a developer-agent gateway. Tenants that require name detection may opt
in explicitly after adding contextual inspection rules and corpus coverage; the
gateway still maps configured `PERSON_NAME` findings to the legacy `PERSON` type.

Treat changes to this list or likelihood threshold as security-policy changes and rerun the evaluation corpus.

## Per-tenant PERSON/name-detection opt-in: feasibility assessment (`ai-gateway-6xx`)

Today, disabling PERSON/name detection is a single, process-wide default applied identically to every tenant and every request on both backends. There is no per-tenant or per-request override:

- **Presidio**: `governance/app/pii.py`'s `_DISABLED_PRESIDIO_ENTITIES` frozenset unconditionally filters `PERSON` out of every `scan()` call's results, regardless of caller.
- **Google DLP**: `PERSON_NAME` is simply absent from the default `GOOGLE_DLP_INFO_TYPES` allowlist (see table above). `_google_info_types` is resolved once in `initialize()` for the life of the process, not per request.

This section assesses whether `config/users.yaml` or request metadata could realistically carry a flag to re-enable PERSON detection for one tenant, as requested by `ai-gateway-6xx`.

**Can `config/users.yaml` carry it?** Its schema is `id`, `tenant_id`, `roles`, `initial_key` — no feature-flag field exists today. More importantly, this file is IaC-only: it is read solely by `scripts/provision.py` and `scripts/onboard.py` at provisioning time and reconciled into Postgres and OPA data documents (`policies/data/users.json`). Neither `governance/app/pii.py` nor `governance/app/pipeline.py` reads it, or anything derived from it, at request time. Adding a field there would require a new delivery path end-to-end, not just a schema addition. That said, `roles` is already a *live* per-request channel — it flows `config/users.yaml` -> API-key auth -> proxy `CallerContext.roles` -> `InspectRequest.roles` -> governance `PipelineContext.roles` — with zero new schema needed. A role convention (e.g. `pii:person-detection-enabled`) could ride this existing channel with no new plumbing. However, `PipelineContext.roles` is never read inside `pii_stage()` or `pii.py` today, so this is unused capacity, not a wired path — and a per-user role is arguably the wrong scope for a compliance-sensitive detector policy that a tenant admin, not an individual end user, should control.

**Can `config/tenants.yaml` carry it?** This is the more natural fit, and the ticket's premise (that only `users.yaml` could serve) undersells what already exists: `config/tenants.yaml` already has per-tenant PII policy fields — `pii_action` (`redact`/`pass`) and `pii_redaction_notification` (`header`/`silent`) — reconciled by `scripts/provision.py` into a Postgres `tenants` table and read per-request by the proxy's `get_tenant_info()`. This is a working, established precedent for tenant-scoped PII policy. Its limitation today: that tenant data is consumed entirely inside the **proxy** (for response headers and the `/v1/me` `pii_policy` field) and is never forwarded to governance's `/inspect` call — governance currently has no visibility into any tenant-level PII policy at all.

Wiring a new field (e.g. `pii_disabled_entities_override` or a positive `pii_enabled_entities`) through to the detector would need, following the exact pattern `pii_action`/`pii_redaction_notification` already established:

1. A schema addition to `config/tenants.yaml`.
2. Reconciliation of the new column in `scripts/provision.py`'s Postgres `tenants` table.
3. A `SELECT` addition in the proxy's `get_tenant_info()`.
4. A new field on `proxy/app/governance_client.py`'s `InspectRequest` dataclass, populated at the `/v1/messages`-style call site in `proxy/app/main.py`.
5. A new field on governance `app/main.py`'s `InspectRequest` Pydantic model.
6. A new field on `governance/app/context.py`'s `PipelineContext`.
7. `governance/app/pipeline.py`'s `pii_stage()` passing the value into `pii_module.run()`.
8. `governance/app/pii.py`'s `run()`/`scan()` accepting an optional per-call override to merge with (or replace) `_DISABLED_PRESIDIO_ENTITIES`.
9. For parity on the Google backend: equivalent per-call `info_types` override plumbing in `run()`/`run_google()`, since `_google_info_types` is presently fixed once at `initialize()` time for the whole process — this is the larger, currently-unbuilt half of the work.

Each step is small and mechanical, so **this is feasible**, via `config/tenants.yaml` (preferred over `config/users.yaml` or ad hoc per-request metadata — a per-request override would let any caller holding a valid key weaken PII detection for their own traffic, which is a weaker control than a tenant admin-set policy).

**Is it desired right now? No — deferred, not rejected.** Reasoning:

1. No tenant has actually requested PERSON/name detection; this is speculative, nice-to-have work (P4), not a response to a concrete need.
2. Flipping the flag on does not fix the underlying detector behavior — it only exposes the same false-positive corpus (Django/Gemini/Claude-style misclassification, confirmed on both Presidio and the live Google DLP evaluation above) to whichever tenant opts in. This document already states Google-side opt-in additionally requires "adding contextual inspection rules and corpus coverage" that has not been done. Shipping the toggle before that prerequisite work would hand a tenant a control surface that looks supported but reproduces a known accuracy problem.
3. Presidio is a rollback-only path scheduled for removal once the Google DLP production soak completes (see "Rollout and rollback" and "Production soak evidence" below, tracked as `ai-gateway-fcr`). Building override plumbing that primarily targets `_DISABLED_PRESIDIO_ENTITIES` would be effort spent on code with a scheduled deletion date; a real opt-in needs to target the Google side's `info_types` plumbing (step 9 above), which is the larger, currently-undone half of the work.

Revisit this if a specific tenant requests name detection **and** the Google DLP contextual-rules/corpus prerequisite has been completed. No follow-up issue has been filed for the implementation; if raised again, scope it as tenant-level (`config/tenants.yaml`) opt-in with parity across both backends, not a Presidio-only patch.

## GCP setup

The dedicated developer impersonation path is managed by the standalone
Terraform root at `infra/terraform/google-dlp-dev-access`. It enables
`dlp.googleapis.com`, grants the existing keyless Governance service account
`roles/dlp.user`, and grants a dedicated developer a custom role containing
only `iam.serviceAccounts.getAccessToken` on that exact service account. Follow
that root's README for remote-state bootstrap, import, plan, live proof, and the
Terraform-managed removal of the old administrator TokenCreator member. Do not
make these IAM changes with `gcloud`.

`roles/dlp.user` is the least-privilege predefined role intended to inspect, redact, and de-identify content. Do not grant `roles/dlp.admin` or `roles/dlp.editor` to the runtime.

### Local ADC

Use service-account impersonation for local DLP calls. This produces short-lived
service-account access tokens backed by the developer's source login; it does
not create a service-account private key. The dedicated source account needs
the Terraform-managed custom token-minter role on the exact target service
account, and the target service account needs `roles/dlp.user` on the DLP
project.
The DLP request parent (`GOOGLE_CLOUD_PROJECT`) supplies the billing/quota
project; `gcloud auth application-default set-quota-project` is intentionally
not used because gcloud rejects that command for impersonated ADC.

Set the intended identity in `.envrc`, then create impersonated ADC:

```bash
export GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT="gateway-dlp@PROJECT_ID.iam.gserviceaccount.com"
make google-adc-login
make google-adc-preflight
```

`google-adc-preflight` refreshes credentials and verifies the effective identity
without making a DLP call. It reports only the service-account email and token
expiry. Missing ADC, personal-user ADC, the wrong impersonation target, and
expired source credentials fail with sanitized diagnostics. A developer source
login can still expire and require interactive reauthentication; impersonation
does not make a user login permanently unattended.

The impersonated service-account access token is intentionally short-lived and
normally expires after one hour. ADC refreshes it automatically; extending that
token does not remove interactive login. Google permits at most 12 hours only
for service accounts admitted by the
[`constraints/iam.allowServiceAccountCredentialLifetimeExtension`](https://cloud.google.com/iam/docs/create-short-lived-credentials-direct)
organization policy, and the gcloud CLI does not support requesting that longer
lifetime. Do not use that exception for this gateway.

The login boundary is the developer's Google Workspace Cloud session. Google
Cloud session control allows only **1 through 24 hours**, so it cannot provide a
seven-day ADC session. User-backed ADC is covered by that policy and requires
interactive reauthentication when the configured session expires. See Google's
[Cloud session control documentation](https://support.google.com/a/answer/7576830).

For a local developer who must avoid daily login, the pragmatic one-time admin
change is to identify the OAuth application/client used by ADC in Workspace Apps
access control and add it to the **Trusted apps** list. Google documents this as
a temporary exemption from Cloud session-length constraints for applications
that cannot reauthenticate interactively. This broadens the lifetime of that
user refresh credential, so scope the exemption to the developer's group/OU,
review it explicitly, and retain the sentinel/revocation controls below. It does
not make credentials permanent: administrator revocation, user revocation, or
Google security events can still invalidate them.

For an unattended shared cluster, do not exempt personal ADC. Use Workload
Identity Federation or a platform-attached runtime service account so a
non-human workload identity continuously obtains short-lived credentials.

Verify the IAM bindings without changing them:

```bash
gcloud projects get-iam-policy "$GOOGLE_CLOUD_PROJECT" \
  --flatten='bindings[].members' \
  --filter="bindings.role:roles/dlp.user AND bindings.members:serviceAccount:$GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT" \
  --format='table(bindings.role,bindings.members)'

gcloud iam service-accounts get-iam-policy "$GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT" \
  --project "$GOOGLE_CLOUD_PROJECT" \
  --flatten='bindings[].members' \
  --filter='bindings.role:roles/iam.serviceAccountTokenCreator' \
  --format='table(bindings.role,bindings.members)'
```

The live smoke makes billable API calls and checks both an email positive control
and the exact Django false-positive regression. Unit/contract tests use fake
credentials and a fake DLP client.

#### Optional macOS Keychain storage

macOS Keychain can hold the impersonated ADC JSON as a generic-password blob,
but Google ADC and Docker Compose still require a filesystem path. The helper
stores the durable copy without putting JSON in shell arguments, then
materializes a mode-0600 cache file for `.envrc`/Compose:

```bash
make google-adc-keychain-store
```

The Make target verifies Keychain readback before unlinking gcloud's generated
ADC source file. A later `make google-adc-login` recreates that source if needed.

Then use this in `.envrc`:

```bash
export GOOGLE_APPLICATION_CREDENTIALS_HOST="$(scripts/google_adc_keychain.py materialize \
  --expected-service-account "$GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT")"
export GOOGLE_APPLICATION_CREDENTIALS="$GOOGLE_APPLICATION_CREDENTIALS_HOST"
```

The cache file lives under `~/Library/Caches/ai-gateway/`, not in the repository,
and is mounted read-only. This is a necessary plaintext materialization while
the Linux container uses ADC; Keychain cannot be mounted directly into Docker.
Remove the cache file when local Google-backed containers no longer need it.

The Google Compose override also starts `google-credential-sentinel`. Every 30
seconds it loads the same read-only ADC, verifies that the credential is the
expected impersonated service account, refreshes it through Google IAM, and
atomically publishes a metadata-only status file. It never writes access tokens,
refresh tokens, ADC JSON, or provider diagnostics to the status file or logs.
Governance mounts that status read-only and makes `/health` fail readiness when
it is missing, failed, or stale. The Google Compose override uses `/live` for the
container health check so Swarm/Compose lifecycle management does not create a
credential-failure restart loop. DLP-backed requests fail closed with a
sanitized `503` and `Retry-After`; the proxy does not dispatch uninspected
content upstream.

The sentinel detects and contains expiration; it cannot bypass Google's demand
for interactive reauthentication. If plain gcloud ADC needs renewal, run:

```bash
make google-adc-renew
```

For the Keychain-backed workflow configured above, run:

```bash
make google-adc-keychain-renew
```

The plain target leaves gcloud's mode-0600 ADC in its normal configuration path.
The Keychain target verifies and replaces the Keychain copy, removes gcloud's
source file, and explicitly supplies the newly materialized cache path to
Compose. Both recreate the sentinel and Governance containers because a
bind-mounted file can remain pinned to the old inode after an atomic host-side
replacement.

Docker Compose deliberately defaults to `PII_BACKEND=presidio`. For a Google-backed
local container test, set `GOOGLE_APPLICATION_CREDENTIALS_HOST` to the host impersonated ADC
JSON path and add the opt-in override with
`COMPOSE_FILE=docker-compose.yml:docker-compose.google-dlp.yml`. The override
mounts ADC read-only at `/var/run/gcp/adc.json`. Never copy ADC JSON into the
image or repository. Do not use the override in deployed environments, which
must leave ADC discovery to workload identity rather than personal credentials.

### Production identity

Use the platform's attached service account or Workload Identity Federation so ADC obtains short-lived credentials. Do not create or mount long-lived service-account keys. The deployment must set `PII_BACKEND=google`, project, location, and—when required—the regional API endpoint.

## Data residency

`GOOGLE_DLP_LOCATION` controls the processing-location resource in `projects/{project}/locations/{location}`. If data must also remain in a location while in transit, configure a location that supports a regional endpoint and set `GOOGLE_DLP_API_ENDPOINT` to that documented hostname. A processing parent alone does not prove in-transit regional routing.

Validate the selected location and endpoint against Google's current location table before deployment; support differs by region and feature.

## Cost, quota, and monitoring

Synchronous content inspection is billed by bytes processed and constrained by API quotas. Before production cutover:

- estimate prompt/tool-response bytes per month against current pricing;
- set a project budget and billing alerts;
- monitor DLP request count, latency, errors, quota utilization, and inspected bytes;
- alert on `RESOURCE_EXHAUSTED`, authentication failures, and sustained fail-closed responses; and
- request quota changes before load testing, not during an incident.

Current pricing and quota values are deliberately linked rather than copied because they change:

- [Sensitive Data Protection pricing](https://cloud.google.com/sensitive-data-protection/pricing)
- [Sensitive Data Protection quotas and limits](https://cloud.google.com/dlp/quotas)

## Rollout and rollback

1. Run unit/contract tests with no credentials.
2. Run `make smoke-google-dlp` against the intended project/location.
3. Compare Google and Presidio on the structured-positive and technical-proper-noun corpus.
4. Enable `PII_BACKEND=google` in a non-production environment and observe errors, latency, quota, and cost.
5. Roll out to production and complete the agreed soak (record evidence in "Production soak evidence" below as it becomes available; do not wait until the end of the soak to start filling it in).
6. If rollback is required, explicitly set `PII_BACKEND=presidio` and restart Governance. Never auto-fallback after a Google failure.
7. Remove Presidio, spaCy, model downloads, and rollback configuration after the soak gate is accepted in a separate cleanup change (tracked as `ai-gateway-fcr`), and only once every field in "Production soak evidence" below is filled in with real data and the rollback approval is signed off.

## Production soak evidence (required before removal — `ai-gateway-fcr`)

This section is the sign-off gate for `ai-gateway-fcr` ("Remove Presidio and
spaCy after Google DLP production soak"). Presidio, spaCy, the model
download (`governance/Dockerfile`), local recognizers, and the
`PII_BACKEND=presidio` rollback path must not be removed until every field
below holds real, dated, observed data — not placeholders, not estimates,
not implementation-time smoke-test results — and the rollback approval is
completed.

As of this writing, this table has never been filled in: the Google
Sensitive Data Protection backend was implemented and live-verified against
a one-time evaluation corpus (`ai-gateway-ohg`), but no production soak
window has started or been recorded. Do not treat `ai-gateway-ohg`'s
live-corpus verification as soak evidence — it is a pre-rollout smoke check,
not an elapsed production observation window.

**Soak status:** NOT STARTED (update this line to `IN PROGRESS` or `COMPLETE`
as the soak proceeds, with the date of the update)

| Field | Value | Source / how to obtain it | Filled in by |
|---|---|---|---|
| Soak window (start -> end, UTC) | _TBD_ | Deployment/rollout log or change-ticket timestamps for the production cutover to `PII_BACKEND=google` | Deploy owner / on-call |
| Minimum soak duration agreed vs. met | _TBD_ | State the duration the team agreed to before starting, and whether it was met | Deploy owner / on-call |
| p50 / p95 / p99 `inspectContent` latency | _TBD_ | Governance metrics/dashboards for the soak window (see "Cost, quota, and monitoring" above) | SRE / on-call |
| Availability (success rate; error budget) | _TBD_ | Same dashboards; include counts of `RESOURCE_EXHAUSTED`, authentication failures, and sustained fail-closed responses | SRE / on-call |
| Quota utilization | _TBD_ | Cloud DLP quota console for the soak window | SRE / on-call |
| Cost (billable bytes inspected; spend) | _TBD_ | Cloud Billing export for the soak window | SRE / on-call or finance |
| Structured-PII recall | _TBD_ | Re-run the evaluation corpus (`EMAIL_ADDRESS`, `PHONE_NUMBER`, `US_SOCIAL_SECURITY_NUMBER`, etc.) against soak-period traffic or a repeat corpus pass; report a recall number, not a pass/fail | Engineer who owns the PII backend |
| Technical-name false-positive results | _TBD_ | Repeat the technical-proper-noun corpus check (Django, Flask, FastAPI, PostgreSQL, Kubernetes, React, Gemini, Claude, etc.) against the live Google backend during the soak; report any regressions found | Engineer who owns the PII backend |
| Incidents / rollbacks during soak | _TBD_ | Note every time `PII_BACKEND` was switched back to `presidio` in production during the soak, and why | Deploy owner / on-call |

**Rollback approval (must be explicit; silence or an unfilled table does not count as approval)**

- Approver name: _TBD_
- Approval date: _TBD_
- Explicit statement (paste verbatim once signed): "I have reviewed the production soak evidence above and approve removing Presidio, spaCy, the model download, local recognizers, and the `PII_BACKEND=presidio` rollback configuration."

Until every `_TBD_` above is replaced with real observed data and the
approval fields are completed, `ai-gateway-fcr`'s removal work (deleting
Presidio/spaCy code, dependencies, the model download, and the rollback
configuration) is out of scope and must not be started.

## Official references

- [Sensitive Data Protection client libraries](https://cloud.google.com/sensitive-data-protection/docs/libraries)
- [Inspecting text for sensitive data](https://cloud.google.com/sensitive-data-protection/docs/inspecting-text)
- [Sensitive Data Protection locations](https://cloud.google.com/sensitive-data-protection/docs/locations)
- [Sensitive Data Protection IAM roles](https://cloud.google.com/iam/docs/roles-permissions/dlp)
- [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials)
