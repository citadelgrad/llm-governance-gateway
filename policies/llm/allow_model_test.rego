package llm.allow_model_test

import rego.v1

import data.llm.allow_model

# --- Tier-1 model in tenant's allowed_models: allow regardless of roles ---

test_tier1_in_allowed_models if {
    allow_model.allow with input as {
        "request": {"model": "gpt-5.6-luna"},
        "tenant": {"allowed_models": ["gpt-5.6-luna", "gpt-4o"]},
        "user": {"roles": []},
    }
}

test_tier1_gemini_flash_in_allowed_models if {
    allow_model.allow with input as {
        "request": {"model": "gemini-3.1-flash-lite"},
        "tenant": {"allowed_models": ["gemini-3.1-flash-lite"]},
        "user": {"roles": []},
    }
}

test_tier1_gemini_flash_vertex_in_allowed_models if {
    allow_model.allow with input as {
        "request": {"model": "gemini-3.1-flash-lite-vertex"},
        "tenant": {"allowed_models": ["gemini-3.1-flash-lite-vertex"]},
        "user": {"roles": []},
    }
}

# --- Tier-1 model NOT in allowed_models: deny ---

test_tier1_not_in_allowed_models if {
    not allow_model.allow with input as {
        "request": {"model": "gpt-5.6-luna"},
        "tenant": {"allowed_models": ["gemini-3.1-flash-lite"]},
        "user": {"roles": []},
    }
}

# --- Tier-2 model in allowed_models with tier2-access: allow ---

test_tier2_with_role_in_allowed_models if {
    allow_model.allow with input as {
        "request": {"model": "gpt-4o"},
        "tenant": {"allowed_models": ["gpt-4o"]},
        "user": {"roles": ["tier2-access"]},
    }
}

test_tier2_claude_sonnet_with_role if {
    allow_model.allow with input as {
        "request": {"model": "claude-sonnet-4-6"},
        "tenant": {"allowed_models": ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]},
        "user": {"roles": ["tier2-access"]},
    }
}

test_tier2_claude_sonnet_with_admin_role if {
    allow_model.allow with input as {
        "request": {"model": "claude-sonnet-4-6"},
        "tenant": {"allowed_models": ["claude-sonnet-4-6"]},
        "user": {"roles": ["admin"]},
    }
}

# --- Tier-2 model in allowed_models WITHOUT tier2-access: deny ---

test_tier2_in_allowed_models_no_role if {
    not allow_model.allow with input as {
        "request": {"model": "gpt-4o"},
        "tenant": {"allowed_models": ["gpt-4o"]},
        "user": {"roles": []},
    }
}

# --- Tier-2 model NOT in allowed_models even WITH tier2-access: deny ---

test_tier2_with_role_not_in_allowed_models if {
    not allow_model.allow with input as {
        "request": {"model": "gpt-4o"},
        "tenant": {"allowed_models": ["gpt-5.6-luna"]},
        "user": {"roles": ["tier2-access"]},
    }
}

# --- Unknown model (not in model_tiers): deny even if in allowed_models ---

test_unknown_model_deny if {
    not allow_model.allow with input as {
        "request": {"model": "some-future-model"},
        "tenant": {"allowed_models": ["some-future-model"]},
        "user": {"roles": ["tier2-access"]},
    }
}
