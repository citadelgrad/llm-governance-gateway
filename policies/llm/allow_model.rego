package llm.allow_model

import rego.v1

default allow := false

# model_tiers is duplicated from authz.rego by design: drift between the two
# maps is intentional pain — the test suite asserts they match, so any
# divergence fails CI before it can silently widen or narrow access.
model_tiers := {
    "gpt-4o":            "tier2",
    "gpt-5.6-luna":       "tier1",
    "o1-mini":           "tier2",
    "gpt-3.5-turbo":     "tier1",
    "claude-sonnet-4-6": "tier2",
    "claude-haiku-4-5-20251001": "tier1",
    "claude-opus-4-5":   "tier2",
    "gemini-3.1-flash-lite": "tier1",
    "gemini-3.1-flash-lite-vertex": "tier1",
}

allow if {
    input.request.model in input.tenant.allowed_models
    tier := model_tiers[input.request.model]
    tier == "tier1"
}

allow if {
    input.request.model in input.tenant.allowed_models
    tier := model_tiers[input.request.model]
    tier == "tier2"
    "tier2-access" in input.user.roles
}

allow if {
    input.request.model in input.tenant.allowed_models
    tier := model_tiers[input.request.model]
    tier == "tier2"
    "admin" in input.user.roles
}
