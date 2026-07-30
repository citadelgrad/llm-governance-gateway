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
    glob.match(e.resource_pattern, ["/"], input.context.resource)
}
