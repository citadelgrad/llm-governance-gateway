from __future__ import annotations

import yaml

_PREFIX_MAP: list[tuple[str, str]] = [
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("o4-", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "gemini"),
    ("llama-", "ollama"),
    ("mistral-", "ollama"),
    ("phi-", "ollama"),
    ("qwen-", "ollama"),
]


def load_models_yaml(path: str) -> list[dict]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("models", [])


def resolve_provider(
    model_id: str,
    request_headers: dict[str, str],
    caller_roles: list[str],
    tenant_default_provider: str,
    models_config: list[dict],
) -> tuple[str, str]:
    """Returns (resolved_provider, routing_method)."""

    requested_override = request_headers.get("x-gateway-provider", "").strip()
    if requested_override:
        required_role = f"gateway:provider_override:{requested_override}"
        if required_role in caller_roles:
            return (requested_override, "header_override")
        return ("", "override_denied")

    for entry in models_config:
        if entry.get("id") == model_id:
            return (entry["provider"], "models_yaml")

    for prefix, provider in _PREFIX_MAP:
        if model_id.startswith(prefix):
            return (provider, "prefix_inference")

    if tenant_default_provider:
        return (tenant_default_provider, "tenant_default")

    return ("", "model_not_found")
