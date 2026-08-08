from __future__ import annotations

from pathlib import Path

from proxy.app.routing import load_models_yaml, resolve_provider

REAL_MODELS_YAML = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


def test_real_models_yaml_resolves_gemini_vertex_catalog_entry():
    models_config = load_models_yaml(str(REAL_MODELS_YAML))

    provider, routing_method = resolve_provider(
        "gemini-3.1-flash-lite-vertex", {}, [], "", models_config
    )

    assert provider == "gemini-vertex"
    assert routing_method == "models_yaml"


def test_models_yaml_entry_resolves_gemini_vertex_without_error():
    models_config = [{"id": "gemini-3.1-pro-vertex", "provider": "gemini-vertex"}]

    provider, routing_method = resolve_provider(
        "gemini-3.1-pro-vertex", {}, [], "", models_config
    )

    assert provider == "gemini-vertex"
    assert routing_method == "models_yaml"


def test_bare_gemini_prefix_defaults_to_developer_api():
    provider, routing_method = resolve_provider("gemini-3.1-flash", {}, [], "", [])

    assert provider == "gemini"
    assert routing_method == "prefix_inference"


def test_bare_gemini_prefix_honors_tenant_default_gemini_vertex():
    provider, routing_method = resolve_provider(
        "gemini-3.1-flash", {}, [], "gemini-vertex", []
    )

    assert provider == "gemini-vertex"
    assert routing_method == "prefix_inference"


def test_other_prefixes_are_unaffected_by_gemini_vertex_tenant_default():
    provider, routing_method = resolve_provider("gpt-5.6", {}, [], "gemini-vertex", [])

    assert provider == "openai"
    assert routing_method == "prefix_inference"


def test_header_override_with_permission_selects_gemini_vertex_over_catalog_entry():
    """Epic scenario: an authorized caller's header override wins even when
    the resolved model's catalog entry specifies provider "gemini"."""
    models_config = [{"id": "gemini-3.1-flash-lite", "provider": "gemini"}]

    provider, routing_method = resolve_provider(
        "gemini-3.1-flash-lite",
        {"x-gateway-provider": "gemini-vertex"},
        ["gateway:provider_override:gemini-vertex"],
        "",
        models_config,
    )

    assert provider == "gemini-vertex"
    assert routing_method == "header_override"


def test_header_override_without_permission_is_denied_not_silently_ignored():
    """A caller lacking the gemini-vertex override permission cannot select it
    via header; resolve_provider signals denial rather than a provider."""
    models_config = [{"id": "gemini-3.1-flash-lite", "provider": "gemini"}]

    provider, routing_method = resolve_provider(
        "gemini-3.1-flash-lite",
        {"x-gateway-provider": "gemini-vertex"},
        [],
        "",
        models_config,
    )

    assert provider == ""
    assert routing_method == "override_denied"


def test_header_override_with_unrelated_permission_is_denied():
    """Holding an override permission for a different provider does not
    grant the gemini-vertex override."""
    provider, routing_method = resolve_provider(
        "gemini-3.1-flash-lite",
        {"x-gateway-provider": "gemini-vertex"},
        ["gateway:provider_override:gemini"],
        "",
        [],
    )

    assert provider == ""
    assert routing_method == "override_denied"


def test_real_models_yaml_has_no_google_provider_entries():
    """Regression test for the naming-bug fix: config/models.yaml must never
    reintroduce provider: google."""
    models_config = load_models_yaml(str(REAL_MODELS_YAML))

    assert all(entry.get("provider") != "google" for entry in models_config)


def test_real_models_yaml_gemini_developer_entries_resolve_to_gemini():
    """All Gemini Developer API catalog entries (non-Vertex) resolve to
    provider "gemini", not "google"."""
    models_config = load_models_yaml(str(REAL_MODELS_YAML))
    developer_api_entries = [
        entry
        for entry in models_config
        if entry["id"].startswith("gemini") and entry["provider"] != "gemini-vertex"
    ]

    assert developer_api_entries, "expected at least one Gemini Developer API entry"
    for entry in developer_api_entries:
        provider, routing_method = resolve_provider(entry["id"], {}, [], "", models_config)
        assert provider == "gemini"
        assert routing_method == "models_yaml"
