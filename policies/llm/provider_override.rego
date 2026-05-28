package llm.provider_override

default allow = false

allow {
    perm := sprintf("gateway:provider_override:%s", [input.request.attempted_provider])
    perm in input.user.permissions
}
