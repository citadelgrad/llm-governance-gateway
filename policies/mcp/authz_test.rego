package mcp.authz_test

import rego.v1

import data.mcp.authz

# --- role + resource-pattern match: allow ---

test_resource_pattern_match_allow if {
    authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_acme", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    }
}

# --- differently-formatted but equivalent resource string: same allow decision ---

test_resource_pattern_match_allow_case_and_trailing_slash_insensitive if {
    authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_acme", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "REPO:ORG/Name/", "prior_calls_this_session": 4},
    }
}

# --- multiple trailing slashes must canonicalize the same as one ---

test_resource_pattern_match_allow_multiple_trailing_slashes if {
    authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_acme", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name///", "prior_calls_this_session": 4},
    }
}

# --- role match but resource outside the declared pattern: deny ---

test_resource_pattern_mismatch_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_acme", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "otherorg/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:otherorg/name", "prior_calls_this_session": 4},
    }
}

# --- tool with no resource_pattern declared: allow on role->tool match alone,
# regardless of what context.resource holds ---

test_no_resource_pattern_declared_allow if {
    authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_acme", "roles": ["mcp-role:read-only"]},
        "tool": {"server": "github-mcp", "name": "list_prs", "arguments": {}},
        "context": {"environment": "prod", "resource": "repo:anything/whatever", "prior_calls_this_session": 1},
    }
}

test_no_resource_pattern_declared_allow_missing_resource if {
    authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_acme", "roles": ["mcp-role:read-only"]},
        "tool": {"server": "github-mcp", "name": "list_prs", "arguments": {}},
        "context": {"environment": "prod", "prior_calls_this_session": 1},
    }
}

# --- role has no entitlement for this server/tool at all: deny ---

test_role_without_entitlement_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_acme", "roles": ["mcp-role:read-only"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    }
}

# --- unknown role entirely: deny ---

test_unknown_role_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_acme", "roles": ["mcp-role:unknown"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    }
}

# --- cross-tenant: role and resource pattern both match, but the
# principal's tenant_id does not match the entitlement's tenant_id: deny ---

test_cross_tenant_resource_pattern_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_beta", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    }
}

# --- cross-tenant: role and tool match on a no-resource_pattern entry, but
# the principal's tenant_id does not match the entitlement's tenant_id: deny.
# Tenant scoping is not opt-in the way resource_pattern is — it applies to
# this branch too. ---

test_cross_tenant_no_resource_pattern_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_beta", "roles": ["mcp-role:read-only"]},
        "tool": {"server": "github-mcp", "name": "list_prs", "arguments": {}},
        "context": {"environment": "prod", "resource": "repo:anything/whatever", "prior_calls_this_session": 1},
    }
}

# --- boundary: input.principal.tenant_id missing, null, or empty string
# must deny even though role/tool/resource all otherwise match, fail-closed
# consistent with default allow := false ---

test_missing_tenant_id_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    }
}

test_null_tenant_id_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": null, "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    }
}

test_empty_tenant_id_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    }
}

# --- boundary: an entitlement entry with no tenant scope recorded in policy
# data must deny rather than silently allowing every tenant, even though the
# principal carries a well-formed tenant_id and role/tool/resource all
# otherwise match. Overrides the entitlements data document itself (not
# input) to simulate an entry that never had tenant_id added. ---

test_entitlement_missing_tenant_id_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_acme", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    } with authz.entitlements as {
        "mcp-role:github-write": [
            {"server": "github-mcp", "tool": "create_pr", "resource_pattern": "repo:org/*"},
        ],
    }
}

test_entitlement_null_tenant_id_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "tenant_acme", "roles": ["mcp-role:read-only"]},
        "tool": {"server": "github-mcp", "name": "list_prs", "arguments": {}},
        "context": {"environment": "prod", "resource": "repo:anything/whatever", "prior_calls_this_session": 1},
    } with authz.entitlements as {
        "mcp-role:read-only": [
            {"server": "github-mcp", "tool": "list_prs", "resource_pattern": null, "tenant_id": null},
        ],
    }
}
