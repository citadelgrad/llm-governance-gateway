package llm.authz_test

import rego.v1

import data.llm.authz

# --- Tier-1 model: allow without any special role ---

test_tier1_model_allow if {
    authz.allow with input as {
        "phase": "pre_call",
        "request": {
            "model": "gpt-5.6-luna",
            "provider": "openai",
            "data_classification": [],
            "pii_findings": [],
        },
        "user": {"roles": []},
    }
}

test_tier1_claude_haiku_allow if {
    authz.allow with input as {
        "phase": "pre_call",
        "request": {
            "model": "claude-haiku-4-5-20251001",
            "provider": "anthropic",
            "data_classification": [],
            "pii_findings": [],
        },
        "user": {"roles": []},
    }
}

# --- Tier-2 model: deny when tier2-access role is absent ---

test_tier2_deny_without_role if {
    not authz.allow with input as {
        "phase": "pre_call",
        "request": {
            "model": "gpt-4o",
            "provider": "openai",
            "data_classification": [],
            "pii_findings": [],
        },
        "user": {"roles": []},
    }
}

test_tier2_deny_wrong_role if {
    not authz.allow with input as {
        "phase": "pre_call",
        "request": {
            "model": "claude-3-5-sonnet",
            "provider": "anthropic",
            "data_classification": [],
            "pii_findings": [],
        },
        "user": {"roles": ["tenant_admin"]},
    }
}

# --- Tier-2 model: allow when tier2-access role is present ---

test_tier2_allow_with_role if {
    authz.allow with input as {
        "phase": "pre_call",
        "request": {
            "model": "gpt-4o",
            "provider": "openai",
            "data_classification": [],
            "pii_findings": [],
        },
        "user": {"roles": ["tier2-access"]},
    }
}

# --- Unknown model (not in model_tiers): deny regardless of roles ---

test_unknown_model_deny if {
    not authz.allow with input as {
        "phase": "pre_call",
        "request": {
            "model": "some-future-model",
            "provider": "openai",
            "data_classification": [],
            "pii_findings": [],
        },
        "user": {"roles": ["tier2-access"]},
    }
}

# --- CRITICAL: PHI + non-approved provider + tier2-access user ---
# allow=true AND deny non-empty: pipeline MUST block on non-empty deny.
# This is the belt-and-suspenders case: a future allow path could exist while
# PHI is still headed to an unapproved provider — deny catches it.

test_phi_unapproved_provider_deny_allow_coexist if {
    mock_input := {
        "phase": "pre_call",
        "request": {
            "model": "claude-3-5-sonnet",
            "provider": "openai",
            "data_classification": ["PHI"],
            "pii_findings": [],
        },
        "user": {"roles": ["tier2-access"]},
    }
    authz.allow with input as mock_input
    result_deny := authz.deny with input as mock_input
    count(result_deny) > 0
    "PHI cannot be sent to unapproved external providers" in result_deny
}

# --- PHI + tier1 model via non-approved provider: allow=true, deny non-empty ---

test_phi_unapproved_tier1_deny_allow_coexist if {
    mock_input := {
        "phase": "pre_call",
        "request": {
            "model": "gpt-5.6-luna",
            "provider": "openai",
            "data_classification": ["PHI"],
            "pii_findings": [],
        },
        "user": {"roles": []},
    }
    authz.allow with input as mock_input
    result_deny := authz.deny with input as mock_input
    count(result_deny) > 0
}

# --- PHI + approved providers: allow with empty deny ---

test_phi_azure_openai_no_deny if {
    result := authz.deny with input as {
        "phase": "pre_call",
        "request": {
            "model": "gpt-4o",
            "provider": "azure-openai",
            "data_classification": ["PHI"],
            "pii_findings": [],
        },
        "user": {"roles": ["tier2-access"]},
    }
    count(result) == 0
}

test_phi_bedrock_no_deny if {
    result := authz.deny with input as {
        "phase": "pre_call",
        "request": {
            "model": "claude-3-5-sonnet",
            "provider": "bedrock",
            "data_classification": ["PHI"],
            "pii_findings": [],
        },
        "user": {"roles": ["tier2-access"]},
    }
    count(result) == 0
}

# --- Non-PHI data: deny is always empty regardless of provider ---

test_non_phi_no_deny if {
    result := authz.deny with input as {
        "phase": "pre_call",
        "request": {
            "model": "gpt-5.6-luna",
            "provider": "openai",
            "data_classification": ["PII"],
            "pii_findings": [],
        },
        "user": {"roles": []},
    }
    count(result) == 0
}

test_no_classification_no_deny if {
    result := authz.deny with input as {
        "phase": "pre_call",
        "request": {
            "model": "gpt-5.6-luna",
            "provider": "openai",
            "data_classification": [],
            "pii_findings": [],
        },
        "user": {"roles": []},
    }
    count(result) == 0
}

# --- redact_pii: enabled when findings exist and action == "redact" ---

test_redact_pii_enabled if {
    authz.redact_pii with input as {
        "phase": "pre_call",
        "request": {
            "model": "gpt-5.6-luna",
            "provider": "openai",
            "data_classification": [],
            "pii_findings": ["SSN"],
        },
        "user": {"roles": []},
        "pipeline": {"pii": {"action": "redact"}},
    }
}

# --- redact_pii: disabled when pii_findings is empty ---

test_redact_pii_no_findings if {
    not authz.redact_pii with input as {
        "phase": "pre_call",
        "request": {
            "model": "gpt-5.6-luna",
            "provider": "openai",
            "data_classification": [],
            "pii_findings": [],
        },
        "user": {"roles": []},
        "pipeline": {"pii": {"action": "redact"}},
    }
}

# --- redact_pii: disabled when action != "redact" ---

test_redact_pii_action_flag if {
    not authz.redact_pii with input as {
        "phase": "pre_call",
        "request": {
            "model": "gpt-5.6-luna",
            "provider": "openai",
            "data_classification": [],
            "pii_findings": ["email"],
        },
        "user": {"roles": []},
        "pipeline": {"pii": {"action": "flag"}},
    }
}
