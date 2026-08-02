package mcp.authz_test

import rego.v1

import data.mcp.authz

# --- role + resource-pattern match: allow ---

test_resource_pattern_match_allow if {
    authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "acme-corp", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    }
}

# --- differently-formatted but equivalent resource string: same allow decision ---

test_resource_pattern_match_allow_case_and_trailing_slash_insensitive if {
    authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "acme-corp", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "REPO:ORG/Name/", "prior_calls_this_session": 4},
    }
}

# --- multiple trailing slashes must canonicalize the same as one ---

test_resource_pattern_match_allow_multiple_trailing_slashes if {
    authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "acme-corp", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name///", "prior_calls_this_session": 4},
    }
}

# --- role match but resource outside the declared pattern: deny ---

test_resource_pattern_mismatch_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "acme-corp", "roles": ["mcp-role:github-write"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "otherorg/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:otherorg/name", "prior_calls_this_session": 4},
    }
}

# --- tool with no resource_pattern declared: allow on role->tool match alone,
# regardless of what context.resource holds ---

test_no_resource_pattern_declared_allow if {
    authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "acme-corp", "roles": ["mcp-role:read-only"]},
        "tool": {"server": "github-mcp", "name": "list_prs", "arguments": {}},
        "context": {"environment": "prod", "resource": "repo:anything/whatever", "prior_calls_this_session": 1},
    }
}

test_no_resource_pattern_declared_allow_missing_resource if {
    authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "acme-corp", "roles": ["mcp-role:read-only"]},
        "tool": {"server": "github-mcp", "name": "list_prs", "arguments": {}},
        "context": {"environment": "prod", "prior_calls_this_session": 1},
    }
}

# --- role has no entitlement for this server/tool at all: deny ---

test_role_without_entitlement_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "acme-corp", "roles": ["mcp-role:read-only"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    }
}

# --- unknown role entirely: deny ---

test_unknown_role_deny if {
    not authz.allow with input as {
        "principal": {"user_id": "user_01HXKP2M", "tenant_id": "acme-corp", "roles": ["mcp-role:unknown"]},
        "tool": {"server": "github-mcp", "name": "create_pr", "arguments": {"repo": "org/name", "base": "main"}},
        "context": {"environment": "prod", "resource": "repo:org/name", "prior_calls_this_session": 4},
    }
}
