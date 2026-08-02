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
| `GOOGLE_DLP_MIN_LIKELIHOOD` | no | `VERY_UNLIKELY` through `VERY_LIKELY`; default `POSSIBLE` |
| `GOOGLE_DLP_TIMEOUT_SECONDS` | no | Per-call timeout/retry deadline, 0-30 seconds; default 5 |
| `GOOGLE_DLP_INFO_TYPES` | no | Comma-separated detector allowlist |
| `GOOGLE_APPLICATION_CREDENTIALS_HOST` | local Compose only | Host ADC path mounted read-only into Governance |

The default detector set is:

`EMAIL_ADDRESS,PHONE_NUMBER,US_SOCIAL_SECURITY_NUMBER,CREDIT_CARD_NUMBER,IP_ADDRESS,STREET_ADDRESS,DATE_OF_BIRTH`

`PERSON_NAME` is intentionally not enabled by default. Live evaluation showed that
Google correctly left `Django` alone but still classified `Gemini` as a possible
person and `Claude` as a likely person. A bare name is too ambiguous to redact
safely in a developer-agent gateway. Tenants that require name detection may opt
in explicitly after adding contextual inspection rules and corpus coverage; the
gateway still maps configured `PERSON_NAME` findings to the legacy `PERSON` type.

Treat changes to this list or likelihood threshold as security-policy changes and rerun the evaluation corpus.

## GCP setup

Enable the API in the dedicated privacy project:

```bash
gcloud services enable dlp.googleapis.com --project "$GOOGLE_CLOUD_PROJECT"
```

Grant the Governance runtime service account the predefined DLP User role:

```bash
gcloud projects add-iam-policy-binding "$GOOGLE_CLOUD_PROJECT" \
  --member="serviceAccount:$GOVERNANCE_SERVICE_ACCOUNT" \
  --role="roles/dlp.user"
```

`roles/dlp.user` is the least-privilege predefined role intended to inspect, redact, and de-identify content. Do not grant `roles/dlp.admin` or `roles/dlp.editor` to the runtime.

### Local ADC

Authenticate application code separately from the `gcloud` CLI:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project "$GOOGLE_CLOUD_PROJECT"
PII_BACKEND=google make smoke-google-dlp
```

The live smoke makes billable API calls and checks both an email positive control and the exact Django false-positive regression. Unit/contract tests use a fake client and require no cloud credentials.

Docker Compose deliberately defaults to `PII_BACKEND=presidio`. For a Google-backed
local container test, set `GOOGLE_APPLICATION_CREDENTIALS_HOST` to the host ADC
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
5. Roll out to production and complete the agreed soak.
6. If rollback is required, explicitly set `PII_BACKEND=presidio` and restart Governance. Never auto-fallback after a Google failure.
7. Remove Presidio, spaCy, model downloads, and rollback configuration after the soak gate is accepted in a separate cleanup change.

## Official references

- [Sensitive Data Protection client libraries](https://cloud.google.com/sensitive-data-protection/docs/libraries)
- [Inspecting text for sensitive data](https://cloud.google.com/sensitive-data-protection/docs/inspecting-text)
- [Sensitive Data Protection locations](https://cloud.google.com/sensitive-data-protection/docs/locations)
- [Sensitive Data Protection IAM roles](https://cloud.google.com/iam/docs/roles-permissions/dlp)
- [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials)
