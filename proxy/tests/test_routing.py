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
