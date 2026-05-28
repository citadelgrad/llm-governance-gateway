package llm.provider_override

import rego.v1

default allow := false

allow if {
    perm := sprintf("gateway:provider_override:%s", [input.request.attempted_provider])
    perm in input.user.permissions
}
