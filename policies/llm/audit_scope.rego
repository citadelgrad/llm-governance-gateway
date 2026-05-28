package llm.audit_scope

import rego.v1

default scope := "SELF"

scope := "PLATFORM" if {
    "platform_admin" in input.user.roles
}

# not "platform_admin" guard ensures PLATFORM and TENANT rules never overlap
scope := "TENANT" if {
    "tenant_admin" in input.user.roles
    not "platform_admin" in input.user.roles
}
