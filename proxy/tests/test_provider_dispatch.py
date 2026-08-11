from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from proxy.app.protocol_types import JsonObject, WireProtocol
from proxy.app.provider_dispatch import ProviderClients, dispatch_provider
from proxy.app.providers import anthropic as anthropic_provider
from proxy.app.providers import gemini as gemini_provider
from proxy.app.providers import gemini_vertex as gemini_vertex_provider
from proxy.app.providers import generic as generic_provider
from proxy.app.providers import mock as mock_provider
from proxy.app.providers import ollama as ollama_provider
from proxy.app.providers import openai as openai_provider
from starlette.responses import Response


@dataclass(frozen=True)
class Payload:
    model: str = "test-model"
    stream: bool = False
    protocol: WireProtocol = "openai_chat"
    native_providers: frozenset[str] = frozenset({"openai", "ollama", "generic"})
    native: JsonObject | None = None
    chat: JsonObject | None = None

    def governance_text(self) -> str:
        return "hello"

    def with_redacted_text(self, redacted_text: str) -> Payload:
        return self

    def native_body(self) -> JsonObject:
        return self.native or self._base_body()

    def to_chat_body(self) -> JsonObject:
        return self.chat or self._base_body()

    def _base_body(self) -> JsonObject:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": "hello"}],
            "stream": self.stream,
        }


def _clients() -> ProviderClients:
    return ProviderClients(
        openai_client=object(),  # type: ignore[arg-type]
        anthropic_client=object(),  # type: ignore[arg-type]
        gemini_client=object(),  # type: ignore[arg-type]
        ollama_client=object(),  # type: ignore[arg-type]
        gemini_vertex_client=object(),  # type: ignore[arg-type]
    )


async def _dispatch(provider: str, payload: Payload, **kwargs):
    return await dispatch_provider(
        provider=provider,
        payload=payload,
        model_id=payload.model,
        models_by_id=kwargs.pop("models_by_id", {payload.model: {"id": payload.model, "provider": provider}}),
        lower_headers=kwargs.pop("lower_headers", {}),
        extra_headers=kwargs.pop("extra_headers", {"X-Test": "1"}),
        clients=kwargs.pop("clients", _clients()),
        mock_mode=kwargs.pop("mock_mode", False),
    )


@pytest.mark.parametrize(
    ("provider", "module_name", "expected_usage"),
    [
        ("openai", "openai", "openai"),
        ("anthropic", "anthropic", "openai"),
        ("gemini", "gemini", "openai"),
        ("gemini-vertex", "gemini_vertex", "openai"),
        ("ollama", "ollama", "openai"),
    ],
)
async def test_translated_dispatch_normalizes_chat_results(
    monkeypatch, provider, module_name, expected_usage
):
    modules = {
        "openai": openai_provider,
        "anthropic": anthropic_provider,
        "gemini": gemini_provider,
        "gemini_vertex": gemini_vertex_provider,
        "ollama": ollama_provider,
    }
    adapter = AsyncMock(return_value=Response(content=b"{}", media_type="application/json"))
    monkeypatch.setattr(modules[module_name], "chat_completions", adapter)

    result = await _dispatch(
        provider,
        Payload(native_providers=frozenset()),
    )

    assert result.response_protocol == "openai_chat"
    assert result.usage_provider == expected_usage
    adapter.assert_awaited_once()


async def test_native_openai_responses_dispatch_preserves_protocol_and_beta_header(monkeypatch):
    adapter = AsyncMock(return_value=Response(content=b"{}", media_type="application/json"))
    chat_adapter = AsyncMock(side_effect=AssertionError("chat adapter must not be used"))
    monkeypatch.setattr(openai_provider, "responses", adapter)
    monkeypatch.setattr(openai_provider, "chat_completions", chat_adapter)

    payload = Payload(
        protocol="openai_responses",
        native_providers=frozenset({"openai"}),
        native={"model": "test-model", "input": "hello", "reasoning": {"effort": "high"}},
    )
    result = await _dispatch(
        "openai",
        payload,
        lower_headers={"openai-beta": "responses=v1", "authorization": "Bearer redacted"},
    )

    assert result.response_protocol == "openai_responses"
    assert result.usage_provider == "openai"
    assert adapter.await_args.kwargs["upstream_headers"] == {"openai-beta": "responses=v1"}
    chat_adapter.assert_not_awaited()


async def test_native_anthropic_messages_dispatch_preserves_protocol_and_beta_headers(monkeypatch):
    adapter = AsyncMock(return_value=Response(content=b"{}", media_type="application/json"))
    chat_adapter = AsyncMock(side_effect=AssertionError("chat adapter must not be used"))
    monkeypatch.setattr(anthropic_provider, "messages", adapter)
    monkeypatch.setattr(anthropic_provider, "chat_completions", chat_adapter)

    payload = Payload(
        protocol="anthropic_messages",
        native_providers=frozenset({"anthropic"}),
        native={"model": "test-model", "messages": [], "thinking": {"type": "enabled"}},
    )
    result = await _dispatch(
        "anthropic",
        payload,
        lower_headers={
            "anthropic-beta": "context-1m-2025-08-07",
            "anthropic-version": "2023-06-01",
            "authorization": "Bearer redacted",
        },
    )

    assert result.response_protocol == "anthropic_messages"
    assert result.usage_provider == "anthropic"
    assert adapter.await_args.kwargs["upstream_headers"] == {
        "anthropic-beta": "context-1m-2025-08-07",
        "anthropic-version": "2023-06-01",
    }
    chat_adapter.assert_not_awaited()


async def test_mock_mode_uses_mock_adapter_and_openai_usage(monkeypatch):
    adapter = AsyncMock(return_value=Response(content=b"{}", media_type="application/json"))
    monkeypatch.setattr(mock_provider, "chat_completions", adapter)

    result = await _dispatch("gemini", Payload(), mock_mode=True)

    assert result.response_protocol == "openai_chat"
    assert result.usage_provider == "openai"
    adapter.assert_awaited_once()


async def test_unknown_provider_with_base_url_dispatches_generic(monkeypatch):
    adapter = AsyncMock(return_value=Response(content=b"{}", media_type="application/json"))
    monkeypatch.setattr(generic_provider, "chat_completions", adapter)
    payload = Payload(model="custom-model", native_providers=frozenset())

    result = await _dispatch(
        "custom-openai",
        payload,
        models_by_id={
            "custom-model": {
                "id": "custom-model",
                "provider": "custom-openai",
                "base_url": "https://example.test/v1",
                "api_key": "secret",
            }
        },
    )

    assert result.response_protocol == "openai_chat"
    assert result.usage_provider == "openai"
    assert adapter.await_args.kwargs["base_url"] == "https://example.test/v1"
    assert adapter.await_args.kwargs["api_key"] == "secret"


async def test_unknown_provider_without_base_url_is_rejected_before_adapter(monkeypatch):
    adapter = AsyncMock(side_effect=AssertionError("generic adapter must not be used"))
    monkeypatch.setattr(generic_provider, "chat_completions", adapter)

    with pytest.raises(HTTPException) as exc_info:
        await _dispatch("custom-openai", Payload(native_providers=frozenset()))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"]["type"] == "unsupported_provider"
    adapter.assert_not_awaited()


async def test_unsupported_translated_chat_fields_are_rejected_before_adapter(monkeypatch):
    adapter = AsyncMock(side_effect=AssertionError("gemini adapter must not be used"))
    monkeypatch.setattr(gemini_provider, "chat_completions", adapter)

    with pytest.raises(HTTPException) as exc_info:
        await _dispatch(
            "gemini",
            Payload(
                native_providers=frozenset(),
                chat={
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": False,
                    "response_format": {"type": "json_object"},
                },
            ),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"]["type"] == "unsupported_protocol_translation"
    assert exc_info.value.detail["error"]["details"] == {
        "provider": "gemini",
        "fields": ["response_format"],
    }
    adapter.assert_not_awaited()


async def test_vertex_without_configured_client_returns_clean_error(monkeypatch):
    adapter = AsyncMock(side_effect=AssertionError("vertex adapter must not be used"))
    monkeypatch.setattr(gemini_vertex_provider, "chat_completions", adapter)

    with pytest.raises(HTTPException) as exc_info:
        await _dispatch(
            "gemini-vertex",
            Payload(native_providers=frozenset()),
            clients=ProviderClients(
                openai_client=None,
                anthropic_client=None,
                gemini_client=None,
                ollama_client=None,
                gemini_vertex_client=None,
            ),
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["error"]["type"] == "provider_not_configured"
    adapter.assert_not_awaited()
