package llm.provider_override_test

import rego.v1

import data.llm.provider_override

# --- User has the exact matching provider permission: allow ---

test_allow_with_anthropic_permission if {
    provider_override.allow with input as {
        "request": {"attempted_provider": "anthropic"},
        "user": {"permissions": ["gateway:provider_override:anthropic"]},
    }
}

test_allow_with_openai_permission if {
    provider_override.allow with input as {
        "request": {"attempted_provider": "openai"},
        "user": {"permissions": ["gateway:provider_override:openai", "gateway:provider_override:bedrock"]},
    }
}

test_allow_with_gemini_vertex_permission if {
    provider_override.allow with input as {
        "request": {"attempted_provider": "gemini-vertex"},
        "user": {"permissions": ["gateway:provider_override:gemini-vertex"]},
    }
}

# --- CRITICAL: provider permission is scoped — anthropic permission cannot pivot to gemini ---

test_anthropic_permission_cannot_pivot_to_gemini if {
    not provider_override.allow with input as {
        "request": {"attempted_provider": "gemini"},
        "user": {"permissions": ["gateway:provider_override:anthropic"]},
    }
}

test_gemini_permission_cannot_pivot_to_gemini_vertex if {
    not provider_override.allow with input as {
        "request": {"attempted_provider": "gemini-vertex"},
        "user": {"permissions": ["gateway:provider_override:gemini"]},
    }
}

test_bedrock_permission_cannot_pivot_to_openai if {
    not provider_override.allow with input as {
        "request": {"attempted_provider": "openai"},
        "user": {"permissions": ["gateway:provider_override:bedrock"]},
    }
}

# --- No permissions: deny ---

test_deny_no_permissions if {
    not provider_override.allow with input as {
        "request": {"attempted_provider": "openai"},
        "user": {"permissions": []},
    }
}

# --- Permission for unrelated providers: deny ---

test_deny_unrelated_provider_permissions if {
    not provider_override.allow with input as {
        "request": {"attempted_provider": "azure-openai"},
        "user": {"permissions": [
            "gateway:provider_override:anthropic",
            "gateway:provider_override:bedrock",
        ]},
    }
}
