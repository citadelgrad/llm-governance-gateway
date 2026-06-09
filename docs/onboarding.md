# Onboarding users and routing agents through the gateway

This guide covers two jobs:

1. enroll a user or service account in the LLM Governance Gateway;
2. configure Claude Code, Codex, and Hermes so model traffic goes through the gateway instead of directly to public model APIs.

Important: endpoint configuration only routes model API calls. It does not stop an agent from using separate web/browser tools. Disable those tools in the agent, firewall the host, or both if the requirement is "no internet except the gateway".

## Gateway endpoints and compatibility

Default local endpoint:

```text
http://localhost:8765
```

Production endpoint examples:

```text
https://llm-gateway.example.com
https://llm-gateway.example.com/v1
```

Current gateway API surface:

| Path | Purpose | Status |
|---|---|---|
| `GET /health` | readiness check | implemented |
| `GET /v1/me` | authenticated caller context | implemented |
| `GET /v1/models` | tenant-scoped model list | implemented |
| `POST /v1/chat/completions` | OpenAI-compatible chat completions | implemented |
| `POST /v1/responses` | OpenAI Responses-compatible endpoint for Codex | implemented |
| `POST /v1/keys` | tenant admin key creation | implemented |
| `GET /v1/audit` | tenant admin audit view | implemented |
| `DELETE /v1/users/{user_id}` | tenant admin user deletion workflow | implemented |

Agent compatibility today:

| Agent | Native API it expects | Works with this gateway today? | Notes |
|---|---|---|---|
| Hermes | OpenAI-compatible `/v1/chat/completions` | yes | Use a custom/OpenAI-compatible provider pointed at the gateway. |
| Claude Code | Anthropic Messages: `/v1/messages`, `/v1/messages/count_tokens`, optionally `/v1/models` | not directly | Claude Code can be pointed at an LLM gateway with `ANTHROPIC_BASE_URL`, but this repo does not yet expose the Anthropic Messages endpoints. Add a compatibility endpoint or put an Anthropic-compatible shim in front. |
| Codex CLI | OpenAI Responses API: `/v1/responses` | yes | Use a custom provider with `wire_api = "responses"` and send the gateway API key via `GATEWAY_API_KEY`. |
Claude Code still needs a separate Anthropic Messages-compatible surface. Codex no longer needs a shim for basic prompt traffic.

## Enrollment model

A caller is identified by:

- `tenant_id` - organization or workspace boundary;
- `user_id` - person, bot, CI job, or service account;
- `roles` - authorization tiers such as `admin`, `tier1`, and `tier2`;
- API key or JWT - credential sent with each request.

Tenants and seed users live in:

```text
config/tenants.yaml
config/users.yaml
config/models.yaml
```

`make provision` upserts those files into Postgres and writes OPA data documents under `policies/data/`.

## Enroll a tenant

1. Add the tenant to `config/tenants.yaml`:

```yaml
tenants:
  - id: example-co
    name: "Example Co"
    allowed_models:
      - gpt-4o-mini
      - claude-3-5-sonnet
    rate_limit: 1000
    pii_action: redact
    pii_redaction_notification: true
    default_provider: openai
    contact_email: ai-admin@example.com
```

2. Keep allowed models narrow. The gateway enforces tenant model access from this list.

3. Re-provision:

```bash
make provision
```

4. Verify the tenant is active by authenticating as a user in that tenant and calling `/v1/me`.

## Enroll an admin user

Add an admin seed user to `config/users.yaml`:

```yaml
users:
  - id: admin-example
    tenant_id: example-co
    roles:
      - admin
      - tier1
    initial_key: "REPLACE_IN_PROVISIONER"
```

Run:

```bash
make provision
```

If the user has no existing API key, the provisioner prints a one-time plaintext key:

```text
WARNING: GENERATED API KEYS — STORE THESE NOW, NOT SHOWN AGAIN
  admin-example: gw_...
```

Store it in the user's password manager or secrets manager. Do not commit it to `.env`, `.envrc`, docs, tickets, shell history, or chat.

If you are provisioning in automation, set `SUPPRESS_GENERATED_KEYS=true` and distribute keys through your normal secret delivery path.

## Enroll a normal user or service account

Preferred CLI flow:

```bash
uv --no-config run --with pyyaml scripts/onboard.py add-user \
  --user-id scott-laptop \
  --tenant-id example-co \
  --role tier1

uv --no-config run --with pyyaml scripts/onboard.py add-service-account \
  --account-id ci-release \
  --tenant-id example-co \
  --role tier1

make provision
```

The CLI updates `config/users.yaml`, keeps the provisioner placeholder, and is idempotent. Service accounts are written with a `svc-` prefix and a `service_account` role.

Manual flow:

1. Add the user to `config/users.yaml` so the user exists in the control plane:

```yaml
users:
  - id: scott-laptop
    tenant_id: example-co
    roles:
      - tier1
    initial_key: "REPLACE_IN_PROVISIONER"
```

2. Re-provision:

```bash
make provision
```

3. If the provisioner generated a key, deliver it once and stop.

4. If an admin needs to issue an additional key, call `POST /v1/keys` with an existing tenant admin credential:

```bash
GATEWAY_URL="http://localhost:8765"
ADMIN_KEY="gw_admin_key_from_provisioning"

curl -sS -X POST "$GATEWAY_URL/v1/keys" \
  -H "Authorization: ApiKey $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "scott-laptop",
    "tenant_id": "example-co",
    "roles": ["tier1"]
  }'
```

The response contains the only plaintext copy:

```json
{"key":"..."}
```

Store it immediately.

Notes:

- `POST /v1/keys` requires the caller to have `admin` in `roles`.
- Admins can create keys only inside their own tenant.
- Use `ApiKey <key>` for API keys on the native gateway endpoints. `POST /v1/responses` also accepts `Authorization: Bearer <key>` for Codex compatibility. Other bearer tokens are treated as JWTs.
- API key authentication also works with a bare `Authorization: <key>` header, but `ApiKey` is clearer.

## Verify a user's credential

Use the user's API key, not the admin key:

```bash
GATEWAY_URL="http://localhost:8765"
USER_KEY="gw_or_generated_user_key"

curl -sS "$GATEWAY_URL/v1/me" \
  -H "Authorization: ApiKey $USER_KEY" | python3 -m json.tool
```

Expected shape:

```json
{
  "user_id": "scott-laptop",
  "tenant_id": "example-co",
  "roles": ["tier1"],
  "allowed_models": ["gpt-4o-mini"],
  "rate_limit": {"requests_per_minute": 1000, "resets_at": "..."},
  "pii_policy": {"notification": true}
}
```

Verify a chat completion:

```bash
curl -sS -X POST "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: ApiKey $USER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Reply with gateway-ok"}]
  }' | python3 -m json.tool
```

Useful headers to inspect with `curl -i`:

- `X-Audit-ID`
- `X-Gateway-Pii-Redacted`
- `X-Gateway-Pii-Types`
- `x-ratelimit-limit-requests`
- `x-ratelimit-remaining-requests`
- `x-ratelimit-reset-requests`

## Configure production endpoint snippets

Use the onboarding CLI to generate copy/pasteable config for Hermes, Claude Code, and Codex without embedding a plaintext gateway key:

```bash
uv --no-config run --with pyyaml scripts/onboard.py agent-config \
  --gateway-url https://llm-gateway.example.com \
  --api-key-env GATEWAY_API_KEY \
  --model gpt-4o-mini \
  --claude-model claude-3-5-sonnet
```

The output includes macOS/Linux and Windows PowerShell environment snippets plus the Codex `config.toml` block. It intentionally does not mention any deployment tool; use the same endpoint shape whether you deploy with containers, platform services, or a reverse proxy.

## Configure Hermes

Hermes supports custom/OpenAI-compatible endpoints. Use the gateway base URL with `/v1` and the user's gateway key.

macOS/Linux:

```bash
export GATEWAY_URL="https://llm-gateway.example.com"
export GATEWAY_API_KEY="gw_user_key"

hermes config set model.provider custom
hermes config set model.base_url "$GATEWAY_URL/v1"
hermes config set model.api_key "$GATEWAY_API_KEY"
hermes config set model.default "gpt-4o-mini"
```

Windows PowerShell:

```powershell
$env:GATEWAY_URL = "https://llm-gateway.example.com"
$env:GATEWAY_API_KEY = "gw_user_key"

hermes config set model.provider custom
hermes config set model.base_url "$env:GATEWAY_URL/v1"
hermes config set model.api_key "$env:GATEWAY_API_KEY"
hermes config set model.default "gpt-4o-mini"
```

Restart Hermes after changing provider config. Then verify:

```bash
hermes chat -q "Reply with gateway-ok only."
```

To reduce non-gateway internet use, disable web-capable toolsets for Hermes:

```bash
hermes tools disable web
hermes tools disable search
hermes tools disable browser
```

Tool changes apply to new sessions.

## Configure Claude Code

Claude Code can use an LLM gateway when the gateway exposes the Anthropic Messages API. Required gateway paths are at least:

```text
POST /v1/messages
POST /v1/messages/count_tokens
```

The gateway must preserve or forward Anthropic headers such as:

```text
anthropic-beta
anthropic-version
```

This repository does not currently expose those paths, so the commands below are for a future Anthropic-compatible gateway endpoint or a shim in front of this gateway.

macOS/Linux:

```bash
export ANTHROPIC_BASE_URL="https://llm-gateway.example.com"
export ANTHROPIC_AUTH_TOKEN="gw_user_key"
export ANTHROPIC_MODEL="claude-3-5-sonnet"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1

claude
```

Windows PowerShell:

```powershell
$env:ANTHROPIC_BASE_URL = "https://llm-gateway.example.com"
$env:ANTHROPIC_AUTH_TOKEN = "gw_user_key"
$env:ANTHROPIC_MODEL = "claude-3-5-sonnet"
$env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1"

claude
```

Why `ANTHROPIC_AUTH_TOKEN`: Claude Code sends it as a bearer token, which maps cleanly to gateway-style credentials. If you use `ANTHROPIC_API_KEY`, Claude Code sends it as `x-api-key`; the gateway or shim must translate that to the gateway auth scheme.

If model discovery is enabled, Claude Code queries `/v1/models` on startup for Anthropic Messages gateways. Discovery is not a substitute for `/v1/messages` support.

## Configure Codex CLI

Current Codex CLI custom providers require the OpenAI Responses API wire protocol. The provider must expose:

```text
POST /v1/responses
```

This repository exposes `/v1/responses` for Codex-compatible basic prompt traffic. The compatibility layer reuses the same auth, model allowlist, rate limit, governance, provider dispatch, and audit path as `/v1/chat/completions`.

Create or edit `~/.codex/config.toml` on macOS/Linux, or `%USERPROFILE%\.codex\config.toml` on Windows:

```toml
model = "gpt-4o-mini"
model_provider = "llm-governance-gateway"

[model_providers.llm-governance-gateway]
name = "LLM Governance Gateway"
base_url = "https://llm-gateway.example.com/v1"
wire_api = "responses"
env_key = "GATEWAY_API_KEY"
requires_openai_auth = false

[profiles.gateway]
model_provider = "llm-governance-gateway"
model = "gpt-4o-mini"
```

macOS/Linux:

```bash
export GATEWAY_API_KEY="gw_user_key"
codex -p gateway "Reply with gateway-ok only."
```

Windows PowerShell:

```powershell
$env:GATEWAY_API_KEY = "gw_user_key"
codex -p gateway "Reply with gateway-ok only."
```

Do not set `wire_api = "chat"`; recent Codex builds reject it. Codex should send the gateway key via `GATEWAY_API_KEY`, and the gateway accepts that key through the bearer-token compatibility path on `/v1/responses`.

## Make configuration persistent

### macOS zsh

Use a shell profile only for non-secret defaults:

```bash
cat >> ~/.zshrc <<'EOF'
export GATEWAY_URL="https://llm-gateway.example.com"
export ANTHROPIC_BASE_URL="https://llm-gateway.example.com"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
EOF
```

Put keys in a password manager, 1Password CLI, macOS Keychain, or your organization's secret manager. Avoid raw API keys in `~/.zshrc`.

### Windows PowerShell

Use the user environment for non-secret defaults:

```powershell
[Environment]::SetEnvironmentVariable("GATEWAY_URL", "https://llm-gateway.example.com", "User")
[Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", "https://llm-gateway.example.com", "User")
[Environment]::SetEnvironmentVariable("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", "1", "User")
```

Store keys in Windows Credential Manager, 1Password CLI, or your organization's secret manager. Avoid raw API keys in your PowerShell profile.

## Operator rollout checklist

For each onboarded user:

1. Tenant exists in `config/tenants.yaml` with only approved models.
2. User exists in `config/users.yaml` or has been provisioned in the database.
3. User has exactly the roles needed: no accidental `admin` or `tier2`.
4. API key or JWT was delivered once through a secure channel.
5. `/v1/me` returns the expected user, tenant, roles, models, and rate limit.
6. The intended inference path works through the gateway:
   - `/v1/chat/completions` for OpenAI-compatible chat clients such as Hermes.
   - `/v1/responses` for Codex CLI.
7. Agent config points at the gateway endpoint.
8. Direct provider keys are removed from the user's agent environment.
9. Web/browser tools are disabled or network egress is firewalled if required.
10. Admin verifies audit entries for the onboarding smoke test.

Admin audit check:

```bash
curl -sS "$GATEWAY_URL/v1/audit?limit=20" \
  -H "Authorization: ApiKey $ADMIN_KEY" | python3 -m json.tool
```

## Deprovision a user

1. Remove or disable their API keys in the control plane/database.
2. Remove the user from `config/users.yaml` if it is a seeded account.
3. Run `make provision` so generated OPA data reflects the desired user set.
4. Call the delete workflow if tenant audit/pseudonym cleanup policy requires it:

```bash
curl -sS -X DELETE "$GATEWAY_URL/v1/users/scott-laptop" \
  -H "Authorization: ApiKey $ADMIN_KEY"
```

5. Rotate any shared credentials the user could access.
6. Confirm denied access with `/v1/me` using the old credential.

## Known follow-up work

To make all three agents work directly without shims, the remaining gateway-compatible follow-up is:

- Anthropic Messages compatibility for Claude Code:
  - `POST /v1/messages`
  - `POST /v1/messages/count_tokens`
  - header preservation for `anthropic-beta` and `anthropic-version`
- Auth normalization:
  - translate `ANTHROPIC_AUTH_TOKEN`/`x-api-key` in compatibility endpoints before hitting existing auth.
