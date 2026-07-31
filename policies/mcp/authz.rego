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
entitlements := {
    "mcp-role:github-write": [
        {"server": "github-mcp", "tool": "create_pr", "resource_pattern": "repo:org/*"},
    ],
    "mcp-role:read-only": [
        {"server": "github-mcp", "tool": "list_prs", "resource_pattern": null},
    ],
}

# --- role -> tool match, no resource scoping declared ---

allow if {
    some role in input.principal.roles
    some e in entitlements[role]
    e.server == input.tool.server
    e.tool == input.tool.name
    e.resource_pattern == null
}

# --- role -> tool match, resource scoping declared: context.resource must match ---

allow if {
    some role in input.principal.roles
    some e in entitlements[role]
    e.server == input.tool.server
    e.tool == input.tool.name
    e.resource_pattern != null
    glob.match(canonicalize(e.resource_pattern), ["/"], canonicalize(input.context.resource))
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
