#!/usr/bin/env bash
# Run once per app after `fly apps create`.
# Replace placeholder values with real secrets before executing.
# Never commit actual secrets — use `fly secrets set` or the Fly dashboard.

set -euo pipefail

# Proxy — public-facing app
fly secrets set -a ai-gateway-proxy \
  JWT_SECRET="change-me-min-32-chars-secret-key" \
  GOVERNANCE_INTERNAL_TOKEN="change-me-internal-token" \
  GATEWAY_BOOTSTRAP_TOKEN="change-me-bootstrap-token" \
  PSEUDONYM_HMAC_KEY="change-me-hmac-key-32-chars-min" \
  OPENAI_API_KEY="sk-..." \
  ANTHROPIC_API_KEY="sk-ant-..." \
  GEMINI_API_KEY="AIza..." \
  DATABASE_URL="postgres://gateway:password@top2.nearest.of.ai-gateway-db.internal:5432/gateway"

# Governance — internal only
fly secrets set -a ai-gateway-governance \
  GOVERNANCE_INTERNAL_TOKEN="change-me-internal-token" \
  PSEUDONYM_HMAC_KEY="change-me-hmac-key-32-chars-min" \
  DATABASE_URL="postgres://gateway:password@top2.nearest.of.ai-gateway-db.internal:5432/gateway"

# OPA — internal only, no secrets needed beyond what's baked into policy bundles
# fly secrets set -a ai-gateway-opa ...

# Cron — nightly partition rotation (02:00 UTC)
fly secrets set -a ai-gateway-cron \
  DATABASE_URL="postgres://gateway:password@top2.nearest.of.ai-gateway-db.internal:5432/gateway"
