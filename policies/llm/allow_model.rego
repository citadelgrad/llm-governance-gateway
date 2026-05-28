package llm.allow_model

default allow = false

# model_tiers is duplicated from authz.rego by design: drift between the two
# maps is intentional pain — the test suite asserts they match, so any
# divergence fails CI before it can silently widen or narrow access.
model_tiers := {
    "gpt-4o": "tier2",
    "gpt-4o-mini": "tier1",
    "o1-mini": "tier2",
    "claude-3-5-sonnet": "tier2",
    "claude-3-haiku": "tier1",
    "claude-opus-4-5": "tier2",
    "gemini-1.5-flash": "tier1",
}

allow {
    input.request.model in input.tenant.allowed_models
    tier := model_tiers[input.request.model]
    tier == "tier1"
}

allow {
    input.request.model in input.tenant.allowed_models
    tier := model_tiers[input.request.model]
    tier == "tier2"
    "tier2-access" in input.user.roles
}
