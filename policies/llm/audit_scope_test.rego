package llm.audit_scope_test

import rego.v1

import data.llm.audit_scope

# --- platform_admin → PLATFORM scope ---

test_platform_admin_gets_platform if {
    audit_scope.scope == "PLATFORM" with input as {
        "user": {"roles": ["platform_admin"]},
    }
}

# --- tenant_admin (without platform_admin) → TENANT scope ---

test_tenant_admin_gets_tenant if {
    audit_scope.scope == "TENANT" with input as {
        "user": {"roles": ["tenant_admin"]},
    }
}

# --- platform_admin wins when both roles present; TENANT rule never fires ---

test_platform_admin_beats_tenant_admin if {
    audit_scope.scope == "PLATFORM" with input as {
        "user": {"roles": ["platform_admin", "tenant_admin"]},
    }
}

# --- No recognized role → SELF (default) ---

test_no_roles_defaults_to_self if {
    audit_scope.scope == "SELF" with input as {
        "user": {"roles": []},
    }
}

# --- Unknown role → SELF (default-deny on unknown roles) ---

test_unknown_role_defaults_to_self if {
    audit_scope.scope == "SELF" with input as {
        "user": {"roles": ["analyst", "tier2-access", "some_future_role"]},
    }
}

# --- tier2-access role alone does not elevate scope ---

test_tier2_role_gets_self_scope if {
    audit_scope.scope == "SELF" with input as {
        "user": {"roles": ["tier2-access"]},
    }
}
