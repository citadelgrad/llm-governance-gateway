package llm.authz

import rego.v1

default allow := false
default redact_pii := false

# Tier assignment drives RBAC — tier2 models require the tier2-access role.
# Aliases (claude-sonnet, gpt4, flash) are NOT listed here; the proxy resolves
# them to canonical IDs before calling /inspect, so input.request.model is
# always a canonical ID by the time this policy evaluates.
model_tiers := {
    "gpt-4o":            "tier2",
    "gpt-5.6-luna":       "tier1",
    "o1-mini":           "tier2",
    "gpt-3.5-turbo":     "tier1",
    "claude-3-5-sonnet": "tier2",
    "claude-haiku-4-5-20251001": "tier1",
    "claude-opus-4-5":   "tier2",
    "gemini-3.1-flash-lite": "tier1",
}

# Providers that have signed a HIPAA BAA — the only ones permitted to receive PHI.
phi_approved_providers := {"azure-openai", "bedrock"}

# --- pre_call: model access ---

allow if {
    input.phase == "pre_call"
    tier := model_tiers[input.request.model]
    tier == "tier1"
}

allow if {
    input.phase == "pre_call"
    tier := model_tiers[input.request.model]
    tier == "tier2"
    "tier2-access" in input.user.roles
}

# --- pre_call: PHI provider gate ---
# deny is a set; a non-empty deny should be treated as a hard block by the
# caller even when allow == true (belt-and-suspenders for future allow paths).

deny contains msg if {
    input.phase == "pre_call"
    "PHI" in input.request.data_classification
    not input.request.provider in phi_approved_providers
    msg := "PHI cannot be sent to unapproved external providers"
}

# --- PII redaction signal ---
# Evaluated independently of phase; the pipeline may act on this for any phase.

redact_pii if {
    count(input.request.pii_findings) > 0
    input.pipeline.pii.action == "redact"
}
