"""Typed provider/protocol capabilities and official API field inventories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from proxy.app.protocol_types import JsonObject, WireProtocol
from proxy.app.providers._gemini_common import DEVELOPER_API_DIALECT, VERTEX_DIALECT

SupportLevel = Literal["native", "translated", "unsupported"]

OPENAI_CHAT_FIELDS = frozenset(
    {
        "audio",
        "frequency_penalty",
        "function_call",
        "functions",
        "logit_bias",
        "logprobs",
        "max_completion_tokens",
        "max_tokens",
        "messages",
        "metadata",
        "modalities",
        "model",
        "moderation",
        "n",
        "parallel_tool_calls",
        "prediction",
        "presence_penalty",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "reasoning_effort",
        "response_format",
        "safety_identifier",
        "seed",
        "service_tier",
        "stop",
        "store",
        "stream",
        "stream_options",
        "temperature",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "user",
        "verbosity",
        "web_search_options",
    }
)

OPENAI_RESPONSES_FIELDS = frozenset(
    {
        "background",
        "context_management",
        "conversation",
        "include",
        "input",
        "instructions",
        "max_output_tokens",
        "max_tool_calls",
        "metadata",
        "model",
        "moderation",
        "parallel_tool_calls",
        "previous_response_id",
        "prompt",
        "prompt_cache_key",
        "prompt_cache_options",
        "prompt_cache_retention",
        "reasoning",
        "safety_identifier",
        "service_tier",
        "store",
        "stream",
        "stream_options",
        "temperature",
        "text",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "truncation",
        "user",
    }
)

# Codex CLI sends this OpenAI-owned extension even though it is not currently
# exposed by the public openai-python Responses request type.
CODEX_RESPONSES_EXTENSION_FIELDS = frozenset({"client_metadata"})

# Claude Code uses the beta Messages surface. Keep beta-only request fields
# distinct from the stable SDK inventory so drift checks remain meaningful.
ANTHROPIC_BETA_MESSAGES_EXTENSION_FIELDS = frozenset({"context_management"})

ANTHROPIC_MESSAGES_FIELDS = frozenset(
    {
        "cache_control",
        "container",
        "inference_geo",
        "max_tokens",
        "messages",
        "metadata",
        "model",
        "output_config",
        "service_tier",
        "stop_sequences",
        "stream",
        "system",
        "temperature",
        "thinking",
        "tool_choice",
        "tools",
        "top_k",
        "top_p",
        "user_profile_id",
    }
)

GEMINI_GENERATE_CONFIG_FIELDS = frozenset(
    {
        "audio_timestamp",
        "audio_transcription_config",
        "automatic_function_calling",
        "cached_content",
        "candidate_count",
        "enable_enhanced_civic_answers",
        "frequency_penalty",
        "http_options",
        "image_config",
        "labels",
        "logprobs",
        "max_output_tokens",
        "media_resolution",
        "model_armor_config",
        "model_selection_config",
        "presence_penalty",
        "response_json_schema",
        "response_logprobs",
        "response_mime_type",
        "response_modalities",
        "response_schema",
        "routing_config",
        "safety_settings",
        "seed",
        "service_tier",
        "should_return_http_response",
        "speech_config",
        "stop_sequences",
        "system_instruction",
        "temperature",
        "thinking_config",
        "tool_config",
        "tools",
        "top_k",
        "top_p",
    }
)

ANTHROPIC_CHAT_TRANSLATION_FIELDS = frozenset(
    {
        "max_completion_tokens",
        "max_tokens",
        "messages",
        "model",
        "stop",
        "stream",
        "temperature",
        "tool_choice",
        "tools",
        "top_p",
    }
)

GEMINI_CHAT_TRANSLATION_FIELDS = frozenset(
    {
        "max_completion_tokens",
        "max_tokens",
        "messages",
        "model",
        "parallel_tool_calls",
        "stop",
        "stream",
        "temperature",
        "tool_choice",
        "tools",
        "top_p",
    }
)


@dataclass(frozen=True)
class ProviderCapabilities:
    native_protocols: frozenset[WireProtocol]
    chat_translation_fields: frozenset[str]
    # Backend-specific Gemini `finishReason` values (response-shape only;
    # not consulted by unsupported_chat_fields()). Empty for non-Gemini
    # providers.
    extra_finish_reasons: frozenset[str] = frozenset()


PROVIDER_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "anthropic": ProviderCapabilities(
        native_protocols=frozenset({"anthropic_messages"}),
        chat_translation_fields=ANTHROPIC_CHAT_TRANSLATION_FIELDS,
    ),
    "gemini": ProviderCapabilities(
        native_protocols=frozenset(),
        chat_translation_fields=GEMINI_CHAT_TRANSLATION_FIELDS,
        extra_finish_reasons=DEVELOPER_API_DIALECT.extra_finish_reasons,
    ),
    "gemini-vertex": ProviderCapabilities(
        native_protocols=frozenset(),
        chat_translation_fields=GEMINI_CHAT_TRANSLATION_FIELDS,
        extra_finish_reasons=VERTEX_DIALECT.extra_finish_reasons,
    ),
    "openai": ProviderCapabilities(
        native_protocols=frozenset({"openai_chat", "openai_responses"}),
        chat_translation_fields=OPENAI_CHAT_FIELDS,
    ),
    "ollama": ProviderCapabilities(
        native_protocols=frozenset({"openai_chat"}),
        chat_translation_fields=OPENAI_CHAT_FIELDS,
    ),
    "generic": ProviderCapabilities(
        native_protocols=frozenset({"openai_chat"}),
        chat_translation_fields=OPENAI_CHAT_FIELDS,
    ),
    "mock": ProviderCapabilities(
        native_protocols=frozenset({"openai_chat"}),
        chat_translation_fields=OPENAI_CHAT_FIELDS,
    ),
}


def unsupported_chat_fields(provider: str, body: JsonObject) -> list[str]:
    """Return populated Chat fields the target adapter cannot preserve."""
    capabilities = PROVIDER_CAPABILITIES.get(provider)
    if capabilities is None:
        return sorted(body)
    return sorted(
        key
        for key, value in body.items()
        if key not in capabilities.chat_translation_fields and value is not None
    )
