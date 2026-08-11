from __future__ import annotations

import pytest
from proxy.app.protocol_types import (
    CanonicalExecutionRequest,
    CanonicalFunction,
    CanonicalFunctionTool,
    CanonicalImagePart,
    CanonicalMessage,
    CanonicalRequest,
    CanonicalTextPart,
    ExecutionFunctionCallItem,
    ExecutionFunctionCallOutputItem,
    ExecutionReasoningItem,
    OpenAIChatCompletionChunk,
    OpenAIChatPayload,
    OpenAIChatRequest,
    format_validation_location,
)
from pydantic import ValidationError


def test_canonical_request_preserves_typed_tool_schema() -> None:
    request = CanonicalRequest(
        model="gateway-model",
        messages=[CanonicalMessage(role="user", content="weather?")],
        tools=[
            CanonicalFunctionTool(
                function=CanonicalFunction(
                    name="get_weather",
                    parameters={
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                )
            )
        ],
    )

    assert request.tools[0].function.parameters["required"] == ["city"]


def test_canonical_request_extracts_only_final_user_text() -> None:
    request = CanonicalRequest(
        model="gateway-model",
        messages=[
            CanonicalMessage(role="user", content="first"),
            CanonicalMessage(role="assistant", content="answer"),
            CanonicalMessage(
                role="user",
                content=[CanonicalTextPart(text="line 1\n"), CanonicalTextPart(text="line 2")],
            ),
        ],
    )

    assert request.governance_text() == "line 1\nline 2"


def test_canonical_redaction_does_not_mutate_original_request() -> None:
    request = CanonicalRequest(
        model="gateway-model",
        messages=[CanonicalMessage(role="user", content="My SSN is 123-45-6789")],
    )

    redacted = request.with_redacted_user_text("My SSN is [REDACTED]")

    assert request.messages[0].content == "My SSN is 123-45-6789"
    assert redacted.messages[0].content == "My SSN is [REDACTED]"


def test_canonical_image_requires_exactly_one_source() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        CanonicalImagePart(media_type="image/png")

    with pytest.raises(ValidationError, match="exactly one"):
        CanonicalImagePart(media_type="image/png", data="AA==", url="https://example.test/a.png")


def test_canonical_tool_messages_require_tool_call_id() -> None:
    with pytest.raises(ValidationError, match="tool messages require tool_call_id"):
        CanonicalMessage(role="tool", content="result")


def test_canonical_models_reject_unknown_semantics() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CanonicalRequest.model_validate(
            {
                "model": "gateway-model",
                "messages": [{"role": "user", "content": "hello"}],
                "silently_lost_vendor_option": True,
            }
        )


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


def test_execution_domain_preserves_call_identity_and_encrypted_reasoning() -> None:
    request = CanonicalExecutionRequest(
        model="gateway-model",
        input=[
            ExecutionReasoningItem(
                id="rs_1",
                encrypted_content="opaque-provider-material",
                status="completed",
            ),
            ExecutionFunctionCallItem(
                id="fc_1",
                call_id="call_1",
                name="lookup",
                arguments='{"key":"x"}',
                status="completed",
            ),
            ExecutionFunctionCallOutputItem(
                call_id="call_1",
                output="value",
                status="completed",
            ),
        ],
    )

    assert isinstance(request.input, list)
    reasoning, call, output = request.input
    assert isinstance(reasoning, ExecutionReasoningItem)
    assert isinstance(call, ExecutionFunctionCallItem)
    assert isinstance(output, ExecutionFunctionCallOutputItem)
    assert reasoning.encrypted_content == "opaque-provider-material"
    assert call.call_id == output.call_id == "call_1"


def test_execution_domain_rejects_conflicting_state_references() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        CanonicalExecutionRequest(
            model="gateway-model",
            input="hello",
            previous_response_id="resp_1",
            conversation="conv_1",
        )


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
