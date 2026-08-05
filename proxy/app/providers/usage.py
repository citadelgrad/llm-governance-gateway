from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from proxy.app.protocol_types import JsonObject


@dataclass(frozen=True, slots=True)
class UsageMetrics:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @classmethod
    def zero(cls) -> UsageMetrics:
        return cls(0, 0, 0)


def _usage_object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    return cast(JsonObject, value)


def _token_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def extract_usage(provider: str, response_json: JsonObject) -> UsageMetrics:
    if not isinstance(response_json, dict):
        return UsageMetrics.zero()
    match provider:
        case "openai" | "ollama" | "generic":
            # Chat uses prompt/completion; Responses uses input/output token names.
            usage = _usage_object(response_json.get("usage"))
            prompt = _token_count(
                usage.get("prompt_tokens", usage.get("input_tokens", 0))
            )
            completion = _token_count(
                usage.get("completion_tokens", usage.get("output_tokens", 0))
            )
            reported = _token_count(usage.get("total_tokens", 0))
            total = max(reported, prompt + completion)
            return UsageMetrics(prompt, completion, total)
        case "anthropic":
            # Anthropic-shape: {"usage": {"input_tokens", "output_tokens"}}
            usage = _usage_object(response_json.get("usage"))
            prompt = _token_count(usage.get("input_tokens", 0))
            completion = _token_count(usage.get("output_tokens", 0))
            return UsageMetrics(prompt, completion, prompt + completion)
        case "gemini":
            # Gemini-shape: usageMetadata keys are promptTokenCount / candidatesTokenCount
            usage = _usage_object(response_json.get("usageMetadata"))
            prompt = _token_count(usage.get("promptTokenCount", 0))
            completion = _token_count(usage.get("candidatesTokenCount", 0))
            reported = _token_count(usage.get("totalTokenCount", 0))
            total = max(reported, prompt + completion)
            return UsageMetrics(prompt, completion, total)
        case _:
            return UsageMetrics.zero()
