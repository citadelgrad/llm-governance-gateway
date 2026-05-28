package llm.audit_scope

default scope = "SELF"

scope = "PLATFORM" {
    "platform_admin" in input.user.roles
}

# not "platform_admin" guard ensures PLATFORM and TENANT rules never overlap
scope = "TENANT" {
    "tenant_admin" in input.user.roles
    not "platform_admin" in input.user.roles
}
