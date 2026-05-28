package llm.authz

# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────

default allow := false

default redact_pii := false

# ─────────────────────────────────────────────────────────────────────────────
# Model tier registry
# Maps each model identifier to a capability tier (1 = basic, 3 = advanced).
# ─────────────────────────────────────────────────────────────────────────────

model_tiers := {
    # Tier 1 — general / lightweight
    "gpt-3.5-turbo":        1,
    "gpt-3.5-turbo-16k":   1,
    "claude-haiku-3":       1,
    "claude-haiku-3-5":     1,
    "gemini-1.5-flash":     1,

    # Tier 2 — mid-range
    "gpt-4o-mini":          2,
    "claude-sonnet-3-5":    2,
    "claude-sonnet-4":      2,
    "claude-sonnet-4-5":    2,
    "gemini-1.5-pro":       2,

    # Tier 3 — flagship / advanced reasoning
    "gpt-4o":               3,
    "gpt-4-turbo":          3,
    "o1":                   3,
    "o3":                   3,
    "claude-opus-4":        3,
    "claude-opus-3":        3,
    "gemini-2-0-flash":     3,
    "gemini-ultra":         3,
}

# ─────────────────────────────────────────────────────────────────────────────
# Approved providers for PHI (Protected Health Information) data
# ─────────────────────────────────────────────────────────────────────────────

phi_approved_providers := {"anthropic", "azure-openai"}

# ─────────────────────────────────────────────────────────────────────────────
# Phase-gated allow rules
# ─────────────────────────────────────────────────────────────────────────────

# Phase 1: Allow if the request has a valid API key and targets a known model.
allow if {
    input.phase >= 1
    input.api_key != ""
    model_tiers[input.model]
}

# Phase 2: Additionally enforce that the caller's tier entitlement is sufficient
# for the requested model tier.
allow if {
    input.phase >= 2
    input.api_key != ""
    required_tier := model_tiers[input.model]
    input.caller_tier >= required_tier
}

# Phase 3: Allow trusted internal services regardless of caller tier.
allow if {
    input.phase >= 3
    input.api_key != ""
    input.internal == true
    model_tiers[input.model]
}

# ─────────────────────────────────────────────────────────────────────────────
# Deny rules
# ─────────────────────────────────────────────────────────────────────────────

# Deny PHI data being routed to a non-approved provider.
deny contains msg if {
    input.data_classification == "phi"
    not phi_approved_providers[input.provider]
    msg := sprintf(
        "PHI data may not be sent to provider %q; approved providers: %v",
        [input.provider, phi_approved_providers],
    )
}

# Deny if the requested model is unknown/unregistered.
deny contains msg if {
    not model_tiers[input.model]
    msg := sprintf("model %q is not registered in the model tier registry", [input.model])
}

# ─────────────────────────────────────────────────────────────────────────────
# PII redaction flag
# Set to true when the request carries PII but is routed to a non-PHI-approved
# provider, signalling the proxy to redact before forwarding.
# ─────────────────────────────────────────────────────────────────────────────

redact_pii if {
    input.data_classification == "pii"
    not phi_approved_providers[input.provider]
}
