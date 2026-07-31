#!/usr/bin/env bash
# Operational check for AC7: confirms the running OPA Sidecar evaluates
# policies/mcp/authz.rego the same way policies/mcp/authz_test.rego's cases
# expect. `opa test` is CLI-only and can't target a live server, so this
# replays the same fixtures as HTTP requests instead. The sidecar has no
# published host port (AC4), so requests go through `docker compose exec`
# into mcpproxy, which shares the sidecar's network namespace and can reach
# it at 127.0.0.1:8181. Run after `make up`.
set -euo pipefail

SIDECAR_URL="http://127.0.0.1:8181/v1/data/mcp/authz/allow"
fail=0

check() {
  local name="$1" input="$2" want="$3"
  local body
  body=$(docker compose exec -T mcpproxy curl -sf -X POST "$SIDECAR_URL" \
    -H 'Content-Type: application/json' \
    -d "{\"input\": $input}")
  if echo "$body" | grep -q "\"result\":$want"; then
    echo "PASS: $name"
  else
    echo "FAIL: $name (expected result=$want, got: $body)"
    fail=1
  fi
}

check "resource_pattern_match_allow" \
  '{"principal":{"user_id":"user_01HXKP2M","tenant_id":"tenant_acme","roles":["mcp-role:github-write"]},"tool":{"server":"github-mcp","name":"create_pr","arguments":{"repo":"org/name","base":"main"}},"context":{"environment":"prod","resource":"repo:org/name","prior_calls_this_session":4}}' \
  true

check "resource_pattern_match_allow_case_and_trailing_slash_insensitive" \
  '{"principal":{"user_id":"user_01HXKP2M","tenant_id":"tenant_acme","roles":["mcp-role:github-write"]},"tool":{"server":"github-mcp","name":"create_pr","arguments":{"repo":"org/name","base":"main"}},"context":{"environment":"prod","resource":"REPO:ORG/Name/","prior_calls_this_session":4}}' \
  true

check "resource_pattern_mismatch_deny" \
  '{"principal":{"user_id":"user_01HXKP2M","tenant_id":"tenant_acme","roles":["mcp-role:github-write"]},"tool":{"server":"github-mcp","name":"create_pr","arguments":{"repo":"otherorg/name","base":"main"}},"context":{"environment":"prod","resource":"repo:otherorg/name","prior_calls_this_session":4}}' \
  false

check "no_resource_pattern_declared_allow" \
  '{"principal":{"user_id":"user_01HXKP2M","tenant_id":"tenant_acme","roles":["mcp-role:read-only"]},"tool":{"server":"github-mcp","name":"list_prs","arguments":{}},"context":{"environment":"prod","resource":"repo:anything/whatever","prior_calls_this_session":1}}' \
  true

check "no_resource_pattern_declared_allow_missing_resource" \
  '{"principal":{"user_id":"user_01HXKP2M","tenant_id":"tenant_acme","roles":["mcp-role:read-only"]},"tool":{"server":"github-mcp","name":"list_prs","arguments":{}},"context":{"environment":"prod","prior_calls_this_session":1}}' \
  true

check "role_without_entitlement_deny" \
  '{"principal":{"user_id":"user_01HXKP2M","tenant_id":"tenant_acme","roles":["mcp-role:read-only"]},"tool":{"server":"github-mcp","name":"create_pr","arguments":{"repo":"org/name","base":"main"}},"context":{"environment":"prod","resource":"repo:org/name","prior_calls_this_session":4}}' \
  false

check "unknown_role_deny" \
  '{"principal":{"user_id":"user_01HXKP2M","tenant_id":"tenant_acme","roles":["mcp-role:unknown"]},"tool":{"server":"github-mcp","name":"create_pr","arguments":{"repo":"org/name","base":"main"}},"context":{"environment":"prod","resource":"repo:org/name","prior_calls_this_session":4}}' \
  false

exit $fail
