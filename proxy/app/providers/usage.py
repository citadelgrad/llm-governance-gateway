from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UsageMetrics:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @classmethod
    def zero(cls) -> UsageMetrics:
        return cls(0, 0, 0)


def extract_usage(provider: str, response_json: dict) -> UsageMetrics:
    """Extract token usage from a provider response. Returns zero metrics if absent."""
    if not isinstance(response_json, dict):
        return UsageMetrics.zero()

    match provider:
        case "openai" | "ollama" | "generic":
            # OpenAI-shape: {"usage": {"prompt_tokens", "completion_tokens", "total_tokens"}}
            usage = response_json.get("usage") or {}
            prompt = int(usage.get("prompt_tokens", 0) or 0)
            completion = int(usage.get("completion_tokens", 0) or 0)
            total = int(usage.get("total_tokens", 0) or 0) or (prompt + completion)
            return UsageMetrics(prompt, completion, total)
        case "anthropic":
            # Anthropic-shape: {"usage": {"input_tokens", "output_tokens"}}
            usage = response_json.get("usage") or {}
            prompt = int(usage.get("input_tokens", 0) or 0)
            completion = int(usage.get("output_tokens", 0) or 0)
            return UsageMetrics(prompt, completion, prompt + completion)
        case "gemini":
            # Gemini-shape: usageMetadata keys are promptTokenCount / candidatesTokenCount
            usage = response_json.get("usageMetadata") or {}
            prompt = int(usage.get("promptTokenCount", 0) or 0)
            completion = int(usage.get("candidatesTokenCount", 0) or 0)
            total = int(usage.get("totalTokenCount", 0) or 0) or (prompt + completion)
            return UsageMetrics(prompt, completion, total)
        case _:
            return UsageMetrics.zero()
