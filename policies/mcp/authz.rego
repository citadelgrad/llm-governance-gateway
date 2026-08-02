package mcp.authz

import rego.v1

default allow := false

# Entitlement matrix keyed by role, per docs/auth-architecture.md's "OPA data
# document keyed by role" description. Each entry may declare a
# resource_pattern; when it does, input.context.resource must glob-match it
# in addition to the role->tool match. When resource_pattern is null, the
# tool has no natural single-resource concept (e.g. a listing tool) and is
# governed by role->tool matching alone — resource scoping is opt-in per
# entitlement entry, not mandatory for every tool.
#
# Every entry also declares tenant_id: the tenant this entitlement is scoped
# to. Unlike resource_pattern, tenant scoping is NOT opt-in — every entry
# must carry a non-empty tenant_id, and both allow rules below require it to
# match input.principal.tenant_id (itself required to be a non-empty string)
# before the role/tool/resource match matters at all. Without this, a
# role/pattern match alone would authorize a principal from one tenant to
# reach another tenant's resource — see docs/auth-architecture.md, "tenant_id
# origin and propagation", and same_tenant() below for the fail-closed
# equality check that closes that gap.
entitlements := {
    "mcp-role:github-write": [
        {"server": "github-mcp", "tool": "create_pr", "resource_pattern": "repo:org/*", "tenant_id": "acme-corp"},
    ],
    "mcp-role:read-only": [
        {"server": "github-mcp", "tool": "list_prs", "resource_pattern": null, "tenant_id": "acme-corp"},
    ],
}

# --- role -> tool match, no resource scoping declared ---

allow if {
    some role in input.principal.roles
    some e in entitlements[role]
    e.server == input.tool.server
    e.tool == input.tool.name
    e.resource_pattern == null
    same_tenant(e.tenant_id, input.principal.tenant_id)
}

# --- role -> tool match, resource scoping declared: context.resource must match ---

allow if {
    some role in input.principal.roles
    some e in entitlements[role]
    e.server == input.tool.server
    e.tool == input.tool.name
    e.resource_pattern != null
    glob.match(canonicalize(e.resource_pattern), ["/"], canonicalize(input.context.resource))
    same_tenant(e.tenant_id, input.principal.tenant_id)
}

# canonicalize applies the two normalization steps Rego can express natively —
# lowercasing and trailing-slash stripping — so that differently-formatted but
# equivalent resource strings reach glob.match identically. trim_suffix only
# strips one trailing "/", so a regex is used instead to strip all of them
# (e.g. "repo:org/foo//" must canonicalize the same as "repo:org/foo"). Unicode
# NFC normalization is out of scope here: Rego has no normalization builtin, so
# that step is the MCP Reverse Proxy's responsibility, applied to
# context.resource before this input is constructed (see
# docs/auth-architecture.md, "Resource-string canonicalization").
canonicalize(s) := regex.replace(lower(s), "/+$", "")

# same_tenant is a fail-closed equality check, not a plain ==: both the
# entitlement's tenant_id and the principal's tenant_id must be present,
# non-null, non-empty strings, AND equal to each other. If either side is
# missing, null, or "", is_string()/the != "" check fails, so the whole
# function is undefined rather than true — undefined short-circuits the
# calling allow rule, which falls through to default allow := false, the
# same fail-closed posture as every other check in this file. This is
# deliberately never implemented as "skip the tenant check when tenant_id is
# absent on either side" — that shape would silently allow every tenant
# through an entitlement entry that forgot to record a tenant_id, which is
# exactly the cross-tenant gap this function exists to close.
same_tenant(entitlement_tenant_id, principal_tenant_id) if {
    is_string(entitlement_tenant_id)
    entitlement_tenant_id != ""
    is_string(principal_tenant_id)
    principal_tenant_id != ""
    entitlement_tenant_id == principal_tenant_id
}
