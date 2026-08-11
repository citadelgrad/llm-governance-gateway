from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import httpx
from fastapi import HTTPException
from proxy.app.headers import error_envelope
from proxy.app.protocol_types import (
    GatewayPayload,
    JsonObject,
    ProtocolTranslationError,
    WireProtocol,
)
from proxy.app.provider_capabilities import capability_provider_for, unsupported_chat_fields
from proxy.app.providers import anthropic as anthropic_provider
from proxy.app.providers import gemini as gemini_provider
from proxy.app.providers import gemini_vertex as gemini_vertex_provider
from proxy.app.providers import generic as generic_provider
from proxy.app.providers import mock as mock_provider
from proxy.app.providers import ollama as ollama_provider
from proxy.app.providers import openai as openai_provider
from starlette.responses import Response, StreamingResponse


@dataclass(frozen=True)
class ProviderClients:
    openai_client: httpx.AsyncClient | None
    anthropic_client: httpx.AsyncClient | None
    gemini_client: httpx.AsyncClient | None
    ollama_client: httpx.AsyncClient | None
    gemini_vertex_client: gemini_vertex_provider.VertexGeminiClient | None


@dataclass(frozen=True)
class DispatchResult:
    response: Response | StreamingResponse
    response_protocol: WireProtocol
    usage_provider: str


def _translation_error_type(protocol: WireProtocol) -> str:
    return {
        "anthropic_messages": "unsupported_message_shape",
        "openai_responses": "unsupported_response_shape",
    }.get(protocol, "unsupported_protocol_translation")


def _model_entry(models_by_id: dict[str, JsonObject], model_id: str) -> JsonObject | None:
    return models_by_id.get(model_id)


def _validate_translated_chat_fields(
    provider: str,
    body: JsonObject,
    model_entry: JsonObject | None,
    extra_headers: dict[str, str],
) -> None:
    capability_provider = capability_provider_for(provider, model_entry)
    if capability_provider is None:
        raise HTTPException(
            status_code=400,
            detail=error_envelope("unsupported_provider", f"Provider {provider} not supported"),
            headers=extra_headers,
        )

    unsupported = unsupported_chat_fields(capability_provider, body)
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail=error_envelope(
                "unsupported_protocol_translation",
                f"Provider {provider} cannot preserve Chat fields: " + ", ".join(unsupported),
                details={"provider": provider, "fields": unsupported},
            ),
            headers=extra_headers,
        )


async def dispatch_provider(
    *,
    provider: str,
    payload: GatewayPayload,
    model_id: str,
    models_by_id: dict[str, JsonObject],
    lower_headers: dict[str, str],
    extra_headers: dict[str, str],
    clients: ProviderClients,
    mock_mode: bool,
) -> DispatchResult:
    """Select, validate, invoke, and normalize a provider dispatch."""
    native_dispatch = not mock_mode and provider in payload.native_providers
    try:
        body = payload.native_body() if native_dispatch else payload.to_chat_body()
    except ProtocolTranslationError as exc:
        raise HTTPException(
            status_code=422,
            detail=error_envelope(_translation_error_type(payload.protocol), str(exc)),
            headers=extra_headers,
        ) from exc

    model_entry = _model_entry(models_by_id, model_id)
    if not native_dispatch and not mock_mode:
        _validate_translated_chat_fields(provider, body, model_entry, extra_headers)

    if mock_mode:
        return DispatchResult(
            response=await mock_provider.chat_completions(body, extra_headers),
            response_protocol="openai_chat",
            usage_provider="openai",
        )

    if provider == "openai":
        if native_dispatch and payload.protocol == "openai_responses":
            return DispatchResult(
                response=await openai_provider.responses(
                    cast(httpx.AsyncClient, clients.openai_client),
                    body,
                    payload.stream,
                    extra_headers,
                    upstream_headers={
                        key: value for key, value in lower_headers.items() if key == "openai-beta"
                    },
                ),
                response_protocol="openai_responses",
                usage_provider="openai",
            )
        return DispatchResult(
            response=await openai_provider.chat_completions(
                cast(httpx.AsyncClient, clients.openai_client),
                body,
                payload.stream,
                extra_headers,
            ),
            response_protocol="openai_chat",
            usage_provider="openai",
        )

    if provider == "anthropic":
        if native_dispatch and payload.protocol == "anthropic_messages":
            return DispatchResult(
                response=await anthropic_provider.messages(
                    cast(httpx.AsyncClient, clients.anthropic_client),
                    body,
                    payload.stream,
                    extra_headers,
                    upstream_headers={
                        key: value
                        for key, value in lower_headers.items()
                        if key in {"anthropic-beta", "anthropic-version"}
                    },
                ),
                response_protocol="anthropic_messages",
                usage_provider="anthropic",
            )
        return DispatchResult(
            response=await anthropic_provider.chat_completions(
                cast(httpx.AsyncClient, clients.anthropic_client),
                body,
                payload.stream,
                extra_headers,
            ),
            response_protocol="openai_chat",
            usage_provider="openai",
        )

    if provider == "gemini":
        return DispatchResult(
            response=await gemini_provider.chat_completions(
                cast(httpx.AsyncClient, clients.gemini_client),
                body,
                payload.stream,
                extra_headers,
            ),
            response_protocol="openai_chat",
            usage_provider="openai",
        )

    if provider == "gemini-vertex":
        if clients.gemini_vertex_client is None:
            raise HTTPException(
                status_code=500,
                detail=error_envelope(
                    "provider_not_configured",
                    "gemini-vertex provider is not configured on this gateway",
                ),
                headers=extra_headers,
            )
        return DispatchResult(
            response=await gemini_vertex_provider.chat_completions(
                clients.gemini_vertex_client,
                body,
                payload.stream,
                extra_headers,
            ),
            response_protocol="openai_chat",
            usage_provider="openai",
        )

    if provider == "ollama":
        return DispatchResult(
            response=await ollama_provider.chat_completions(
                cast(httpx.AsyncClient, clients.ollama_client),
                body,
                payload.stream,
                extra_headers,
            ),
            response_protocol="openai_chat",
            usage_provider="openai",
        )

    if model_entry and model_entry.get("base_url"):
        return DispatchResult(
            response=await generic_provider.chat_completions(
                body,
                payload.stream,
                extra_headers,
                base_url=str(model_entry["base_url"]),
                api_key=str(model_entry.get("api_key", "")),
            ),
            response_protocol="openai_chat",
            usage_provider="openai",
        )

    raise HTTPException(
        status_code=400,
        detail=error_envelope("unsupported_provider", f"Provider {provider} not supported"),
        headers=extra_headers,
    )
