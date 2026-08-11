from __future__ import annotations

import pytest
from proxy.app.protocol_types import (
    CanonicalStreamMessageCompleted,
    CanonicalStreamTextDelta,
    CanonicalStreamToolCallStarted,
    OpenAIChatCompletionChunk,
    OpenAIChatPayload,
    OpenAIChatRequest,
    format_validation_location,
)
from pydantic import ValidationError


def test_canonical_stream_events_reject_unknown_semantics() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CanonicalStreamTextDelta.model_validate(
            {"kind": "text_delta", "text": "hello", "wire_finish_reason": "stop"}
        )


def test_canonical_stream_tool_index_is_nonnegative() -> None:
    with pytest.raises(ValidationError, match="greater_than_equal"):
        CanonicalStreamToolCallStarted(tool_index=-1, call_id="call_1", name="lookup")


def test_canonical_stream_terminal_reason_is_provider_neutral() -> None:
    with pytest.raises(ValidationError):
        CanonicalStreamMessageCompleted(reason="tool_calls")

    event = CanonicalStreamMessageCompleted(reason="tool_use")
    assert event.status == "completed"


def test_chat_redaction_preserves_non_text_parts_and_order() -> None:
    payload = OpenAIChatPayload(
        request=OpenAIChatRequest.model_validate(
            {
                "model": "gateway-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "My SSN is 123-45-6789"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.test/a.png"},
                            },
                            {"type": "text", "text": " remains unchanged"},
                        ],
                    }
                ],
            }
        )
    )

    redacted = payload.with_redacted_text("My SSN is [REDACTED] remains unchanged")
    messages = redacted.native_body()["messages"]
    assert isinstance(messages, list)
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "My SSN is [REDACTED]"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.test/a.png"},
    }
    assert content[2] == {"type": "text", "text": " remains unchanged"}


def test_chat_governed_traversal_extracts_and_replaces_final_user_text_parts() -> None:
    payload = OpenAIChatPayload(
        request=OpenAIChatRequest.model_validate(
            {
                "model": "gateway-model",
                "messages": [
                    {"role": "user", "content": "earlier user text"},
                    {"role": "assistant", "content": "answer"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "alpha"},
                            {"type": "text", "text": ""},
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.test/a.png"},
                            },
                            {"type": "text", "text": "omega"},
                        ],
                    },
                ],
            }
        )
    )

    assert payload.governance_text() == "alphaomega"

    redacted = payload.with_redacted_text("alphaomega TAIL")
    messages = redacted.native_body()["messages"]
    assert messages[0]["content"] == "earlier user text"
    content = messages[2]["content"]
    assert content == [
        {"type": "text", "text": "alpha"},
        {"type": "text", "text": ""},
        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
        {"type": "text", "text": "omega TAIL"},
    ]


def test_openai_chat_request_preserves_typed_message_content_and_tools() -> None:
    fixture = {
        "model": "gpt-5.6-luna",
        "messages": [
            {"role": "developer", "content": [{"type": "text", "text": "Be exact."}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.test/a.png", "detail": "high"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    "strict": True,
                },
            },
            {
                "type": "custom",
                "custom": {
                    "name": "run_grammar",
                    "description": "Produce a constrained expression",
                    "format": {
                        "type": "grammar",
                        "grammar": {"definition": "start: WORD", "syntax": "lark"},
                    },
                },
            },
        ],
        "tool_choice": {"type": "function", "function": {"name": "read_file"}},
        "stream": True,
        "stream_options": {"include_usage": True, "include_obfuscation": False},
    }

    request = OpenAIChatRequest.model_validate(fixture)

    assert request.to_json() == fixture
    assert request.messages[1].role == "user"
    assert request.tools is not None
    assert request.tools[0].function.name == "read_file"
    assert request.tools[1].custom.name == "run_grammar"


@pytest.mark.parametrize(
    ("mutated_part", "expected_location"),
    [
        ({"type": "image_url", "image_url": {"url": 3}}, "messages.0.content.1.image_url.url"),
        (
            {"type": "image_url", "image_url": {"url": "ok", "lost": True}},
            "messages.0.content.1.image_url.lost",
        ),
    ],
)
def test_openai_chat_request_rejects_invalid_nested_content_with_precise_location(
    mutated_part: dict[str, object], expected_location: str
) -> None:
    fixture = {
        "model": "gpt-5.6-luna",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "inspect"}, mutated_part],
            }
        ],
    }

    with pytest.raises(ValidationError) as exc_info:
        OpenAIChatRequest.model_validate(fixture)

    locations = {
        format_validation_location(error["loc"])
        for error in exc_info.value.errors(include_input=False, include_url=False)
    }
    assert expected_location in locations


def test_openai_chat_chunk_preserves_tool_deltas_and_usage_only_chunks() -> None:
    tool_chunk = OpenAIChatCompletionChunk.model_validate(
        {
            "id": "chatcmpl_1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gateway-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": ""},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        }
    )
    usage_chunk = OpenAIChatCompletionChunk.model_validate(
        {
            "id": "chatcmpl_1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gateway-model",
            "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )

    assert tool_chunk.choices[0].delta.tool_calls is not None
    assert tool_chunk.choices[0].delta.tool_calls[0].id == "call_1"
    assert usage_chunk.choices == []
    assert usage_chunk.usage is not None
    assert usage_chunk.usage.total_tokens == 5


def test_openai_chat_request_preserves_typed_generation_controls() -> None:
    fixture = {
        "model": "gpt-5.6-luna",
        "messages": [{"role": "user", "content": "hello"}],
        "modalities": ["text", "audio"],
        "audio": {"format": "mp3", "voice": {"id": "voice_1"}},
        "moderation": {
            "model": "omni-moderation-latest",
            "policy": {"input": {"mode": "block"}, "output": {"mode": "score"}},
        },
        "prediction": {"type": "content", "content": [{"type": "text", "text": "known"}]},
        "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        "prompt_cache_retention": "24h",
        "reasoning_effort": "high",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                "strict": True,
            },
        },
        "service_tier": "priority",
        "verbosity": "low",
        "web_search_options": {
            "search_context_size": "medium",
            "user_location": {
                "type": "approximate",
                "approximate": {"city": "Camas", "country": "US"},
            },
        },
    }

    assert OpenAIChatRequest.model_validate(fixture).to_json() == fixture


def test_openai_chat_request_rejects_unknown_modalities() -> None:
    with pytest.raises(ValidationError) as exc_info:
        OpenAIChatRequest.model_validate(
            {
                "model": "gpt-5.6-luna",
                "messages": [{"role": "user", "content": "hello"}],
                "modalities": ["telepathy"],
            }
        )

    assert format_validation_location(exc_info.value.errors()[0]["loc"]) == "modalities.0"
