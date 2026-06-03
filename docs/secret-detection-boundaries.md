# Secret Detection Boundaries

Secret detection belongs in several places, but not every detector belongs in every place. The gateway should protect live LLM traffic; CI and developer tooling should protect source code and release artifacts.

## Responsibility matrix

| Layer | Primary goal | What belongs here | What does not belong here | Recommended tools / implementation | Default posture |
|---|---|---|---|---|---|
| LLM Gateway request path | Prevent accidental exfiltration of secrets to LLM providers | Fast local detection of key-shaped strings, high-confidence token patterns, entropy checks, tenant policy enforcement, redaction/blocking, audit metadata | Full repository scans, git history scans, network verification of credentials, slow scanners, anything that sends candidate secrets to third parties | In-process governance stage: `secret_detection=off/audit/redact/block`; local regex/entropy detectors; OPA policy decisions | `audit + redact` for likely secrets; `block` for high-confidence provider keys in regulated tenants |
| Governance async/background pipeline | Enrich suspicious events without hurting request latency | Optional queued analysis of suspicious prompt/audit events, correlation, SIEM forwarding, operator review workflows | Blocking the user request, verifying arbitrary customer secrets without explicit policy approval | Background worker; internal queue; TruffleHog only in approved internal contexts; audit sink/SIEM integration | Disabled unless explicitly enabled per deployment |
| CI pipeline | Stop leaked credentials from entering public history/releases | Current tree scans, git history scans, verified secret detection, dependency/release gates | Runtime prompt inspection, tenant-specific data decisions | `gitleaks detect --source . --redact`; `trufflehog git file://. --fail --no-update` or equivalent pinned CI invocation | Required on every PR/push |
| Local developer workflow | Catch mistakes before commit/push | Pre-commit/pre-push scans of staged/current files, `.envrc` hygiene, non-key-shaped examples | Expensive full-history scans on every commit; requiring cloud credentials locally | pre-commit hook, `scripts/pre-commit-security.sh`, `gitleaks protect`, optional `trufflehog filesystem .` before release | Fast scan on commit; deeper scan before push/release |
| Release/publication pipeline | Verify publishable artifacts, containers, and git history | Full history scan, current-tree scan, container/build context scan, generated artifact scan, license/security checklist | App-specific runtime policy decisions | Gitleaks + TruffleHog; `git diff --check`; CI; Docker build context review | Required before public release/tag |
| Production operations / SIEM | Detect leaked secrets after deployment and support incident response | Audit event forwarding, suspicious prompt metrics, alerting, incident tickets, key rotation runbooks | Inline request latency work; ad-hoc secret verification without governance approval | Splunk/Honeycomb/Datadog sinks, alert rules, runbooks, optional verified-secret workflow | Alert on high confidence; rotate confirmed exposures |
| Provider / cloud account controls | Limit blast radius if secrets leak | Scoped keys, rotation, provider-side leak detection, secret manager policies, egress monitoring | Relying on gateway scans as the only control | AWS/GCP/OpenAI/Anthropic provider controls, Vault/SOPS/Fly secrets, least privilege | Mandatory defense-in-depth |

## Gateway policy recommendation

The gateway should own live prompt protection, not repository scanning.

Add a lightweight `secret_detection` governance stage that runs before provider dispatch:

1. Detect likely secrets locally.
2. Add structured findings to the governance context.
3. Redact or block based on tenant policy.
4. Write only safe metadata to audit logs: secret type, confidence, span/range, and action taken — not the raw secret.
5. Let OPA decide whether the request is allowed, redacted, or blocked.

Example tenant controls:

```yaml
secret_detection:
  mode: audit        # off | audit | redact | block
  verify: false      # never verify live customer secrets by default
  notify: true
  block_types:
    - aws_access_key
    - private_key
    - openai_api_key
```

## TruffleHog fit

TruffleHog is a strong fit for CI, release, and controlled background analysis. It is not a good fit for the synchronous LLM request path.

Use it for:

- verified secret scanning in CI
- full git-history scans before public releases
- scheduled scans of the public repository
- optional internal-only background verification workflows

Avoid using it for:

- every live prompt
- latency-sensitive request processing
- verifying customer-provided candidate secrets by default
- vendoring/linking TruffleHog code into this Apache-2.0 gateway without a license review

TruffleHog is AGPL-3.0 licensed. Running it as an external CLI in CI is clean. Embedding or deriving code from it inside the gateway is a different legal/product decision and should not be the default.

## Clean boundary

The practical split is:

- Gateway: fast local detection, redaction, blocking, audit metadata.
- CI/release: deep verified scanning with Gitleaks + TruffleHog.
- Developer machine: fast hooks plus optional deep scan before push.
- Ops/SIEM: alerts, triage, and rotation workflows.

That gives strong coverage without turning the gateway into a slow, privacy-weird secret-verification service.
