from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from proxy.app.providers import _gemini_common as gemini_common
from proxy.app.providers._gemini_common import (
    DEVELOPER_API_DIALECT,
    VERTEX_DIALECT,
    GeminiTranslationError,
    extract_message_text,
    is_block_reason_unset,
    iter_openai_chat_sse_from_gemini_lines,
    translate_candidate_to_openai_choice,
    translate_generate_content_response_to_openai_envelope,
    translate_generation_config,
    translate_openai_messages_to_contents,
    translate_tool_choice,
    translate_tools,
    translate_usage_metadata,
)

# ---------------------------------------------------------------------------
# is_block_reason_unset
# ---------------------------------------------------------------------------


def test_is_block_reason_unset_true_for_developer_api_sentinel():
    assert is_block_reason_unset("BLOCK_REASON_UNSPECIFIED", DEVELOPER_API_DIALECT) is True


def test_is_block_reason_unset_false_for_mismatched_sentinel():
    assert is_block_reason_unset("BLOCKED_REASON_UNSPECIFIED", DEVELOPER_API_DIALECT) is False


def test_is_block_reason_unset_true_for_vertex_sentinel():
    assert is_block_reason_unset("BLOCKED_REASON_UNSPECIFIED", VERTEX_DIALECT) is True


def test_is_block_reason_unset_true_for_none():
    assert is_block_reason_unset(None, DEVELOPER_API_DIALECT) is True


# ---------------------------------------------------------------------------
# extract_message_text
# ---------------------------------------------------------------------------


def test_extract_message_text_from_string():
    assert extract_message_text("hi", location="message 0") == "hi"


def test_extract_message_text_from_none():
    assert extract_message_text(None, location="message 0") == ""


def test_extract_message_text_from_parts():
    content = [{"type": "text", "text": "a"}, {"type": "input_text", "text": "b"}]
    assert extract_message_text(content, location="message 0") == "ab"


def test_extract_message_text_rejects_unsupported_part():
    with pytest.raises(GeminiTranslationError, match="not supported"):
        extract_message_text([{"type": "image"}], location="message 0")


# ---------------------------------------------------------------------------
# translate_openai_messages_to_contents
# ---------------------------------------------------------------------------


def test_translate_messages_skips_system_and_developer_roles():
    messages = [
        {"role": "system", "content": "be concise"},
        {"role": "developer", "content": "internal note"},
        {"role": "user", "content": "hi"},
    ]
    contents = translate_openai_messages_to_contents(messages)
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_translate_messages_rejects_unsupported_role():
    with pytest.raises(GeminiTranslationError, match="unsupported role"):
        translate_openai_messages_to_contents([{"role": "bogus", "content": "hi"}])


# ---------------------------------------------------------------------------
# translate_generation_config
# ---------------------------------------------------------------------------


def test_translate_generation_config_maps_known_fields():
    config = translate_generation_config(
        {"temperature": 0.5, "top_p": 0.9, "max_tokens": 100, "stop": "END"}
    )
    assert config == {
        "temperature": 0.5,
        "topP": 0.9,
        "maxOutputTokens": 100,
        "stopSequences": ["END"],
    }


def test_translate_generation_config_empty_when_no_fields_set():
    assert translate_generation_config({}) == {}


# ---------------------------------------------------------------------------
# translate_tools
# ---------------------------------------------------------------------------


def test_translate_tools_none_passthrough():
    assert translate_tools(None) is None


def test_translate_tools_builds_function_declarations():
    tools = translate_tools(
        [{"type": "function", "function": {"name": "lookup", "description": "d"}}]
    )
    assert tools == [
        {"functionDeclarations": [{"name": "lookup", "description": "d"}]}
    ]


def test_translate_tools_rejects_non_list():
    with pytest.raises(GeminiTranslationError, match="tools must be a list"):
        translate_tools("not-a-list")


# ---------------------------------------------------------------------------
# translate_tool_choice
# ---------------------------------------------------------------------------


def test_translate_tool_choice_none_passthrough():
    assert translate_tool_choice(None) is None


def test_translate_tool_choice_auto():
    assert translate_tool_choice("auto") == {"functionCallingConfig": {"mode": "AUTO"}}


def test_translate_tool_choice_none_string_maps_to_none_mode():
    assert translate_tool_choice("none") == {"functionCallingConfig": {"mode": "NONE"}}


def test_translate_tool_choice_required_maps_to_any_mode():
    assert translate_tool_choice("required") == {"functionCallingConfig": {"mode": "ANY"}}


def test_translate_tool_choice_named_function():
    tool_choice = {"type": "function", "function": {"name": "lookup"}}
    assert translate_tool_choice(tool_choice) == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["lookup"]}
    }


def test_translate_tool_choice_rejects_non_function_type():
    with pytest.raises(GeminiTranslationError, match="named function tool_choice"):
        translate_tool_choice({"type": "bogus", "function": {"name": "lookup"}})


def test_translate_tool_choice_rejects_missing_name():
    with pytest.raises(GeminiTranslationError, match="requires a name"):
        translate_tool_choice({"type": "function", "function": {}})


def test_translate_tool_choice_rejects_unsupported_shape():
    with pytest.raises(GeminiTranslationError, match="unsupported Gemini tool_choice"):
        translate_tool_choice(42)


# ---------------------------------------------------------------------------
# translate_candidate_to_openai_choice
# ---------------------------------------------------------------------------


def test_translate_candidate_maps_finish_reason():
    candidate = {"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}
    choice = translate_candidate_to_openai_choice(candidate, DEVELOPER_API_DIALECT, 0)
    assert choice["finish_reason"] == "stop"
    assert choice["message"] == {"role": "assistant", "content": "hi"}
    assert choice["index"] == 0


def test_translate_candidate_allows_dialect_extra_finish_reason():
    candidate = {"content": {"parts": []}, "finishReason": "MODEL_ARMOR"}
    choice = translate_candidate_to_openai_choice(candidate, VERTEX_DIALECT, 0)
    assert choice["finish_reason"] == "content_filter"


def test_translate_candidate_rejects_other_dialects_extra_finish_reason():
    candidate = {"content": {"parts": []}, "finishReason": "MODEL_ARMOR"}
    with pytest.raises(GeminiTranslationError, match="Gemini generation failed"):
        translate_candidate_to_openai_choice(candidate, DEVELOPER_API_DIALECT, 0)


# ---------------------------------------------------------------------------
# translate_usage_metadata
# ---------------------------------------------------------------------------


def test_translate_usage_metadata_maps_fields():
    usage = translate_usage_metadata(
        {"promptTokenCount": 4, "candidatesTokenCount": 6, "totalTokenCount": 10}
    )
    assert usage == {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}


def test_translate_usage_metadata_defaults_missing_fields_to_zero():
    assert translate_usage_metadata({}) == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


# ---------------------------------------------------------------------------
# FINISH_REASON_MAP is a single source of truth
# ---------------------------------------------------------------------------


def test_finish_reason_map_covers_shared_reasons():
    assert gemini_common.FINISH_REASON_MAP == {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
    }


# ---------------------------------------------------------------------------
# extract_block_reason / raise_if_prompt_blocked
# ---------------------------------------------------------------------------


def test_extract_block_reason_returns_none_when_prompt_feedback_missing():
    assert gemini_common.extract_block_reason({}, DEVELOPER_API_DIALECT) is None


def test_extract_block_reason_returns_none_for_unset_sentinel():
    source = {"promptFeedback": {"blockReason": "BLOCK_REASON_UNSPECIFIED"}}
    assert gemini_common.extract_block_reason(source, DEVELOPER_API_DIALECT) is None


def test_extract_block_reason_returns_real_reason():
    source = {"promptFeedback": {"blockReason": "SAFETY"}}
    assert gemini_common.extract_block_reason(source, DEVELOPER_API_DIALECT) == "SAFETY"


def test_raise_if_prompt_blocked_raises_with_provider_label():
    source = {"promptFeedback": {"blockReason": "SAFETY"}}
    with pytest.raises(GeminiTranslationError, match="Widget generation blocked: SAFETY"):
        gemini_common.raise_if_prompt_blocked(source, DEVELOPER_API_DIALECT, provider_label="Widget")


def test_raise_if_prompt_blocked_no_op_when_unset():
    source = {"promptFeedback": {"blockReason": "BLOCKED_REASON_UNSPECIFIED"}}
    gemini_common.raise_if_prompt_blocked(source, VERTEX_DIALECT, provider_label="Vertex")


# ---------------------------------------------------------------------------
# translate_generate_content_response_to_openai_envelope
# ---------------------------------------------------------------------------


_ENVELOPE_CASES = [
    pytest.param(
        DEVELOPER_API_DIALECT,
        "Gemini",
        "chatcmpl-gemini-",
        "BLOCK_REASON_UNSPECIFIED",
        "Gemini generation blocked",
        id="developer-api",
    ),
    pytest.param(
        VERTEX_DIALECT,
        "Vertex",
        "chatcmpl-gemini-vertex-",
        "BLOCKED_REASON_UNSPECIFIED",
        "Vertex generation blocked",
        id="vertex",
    ),
]


@pytest.mark.parametrize(
    ("dialect", "provider_label", "completion_id_prefix", "unset_sentinel", "blocked_message_prefix"),
    _ENVELOPE_CASES,
)
def test_translate_envelope_raises_on_blocked_prompt(
    dialect, provider_label, completion_id_prefix, unset_sentinel, blocked_message_prefix
):
    """An empty candidates list with a genuine promptFeedback.blockReason
    must surface as an error, not a silent empty 'stop' completion."""
    del unset_sentinel  # only used by the sibling default-stop test below
    gemini_json = {
        "candidates": [],
        "promptFeedback": {"blockReason": "SAFETY"},
        "usageMetadata": {},
    }
    with pytest.raises(GeminiTranslationError, match=f"{blocked_message_prefix}: SAFETY"):
        translate_generate_content_response_to_openai_envelope(
            gemini_json,
            model="some-model",
            dialect=dialect,
            provider_label=provider_label,
            completion_id_prefix=completion_id_prefix,
        )


@pytest.mark.parametrize(
    ("dialect", "provider_label", "completion_id_prefix", "unset_sentinel", "blocked_message_prefix"),
    _ENVELOPE_CASES,
)
def test_translate_envelope_default_stop_when_block_reason_unset(
    dialect, provider_label, completion_id_prefix, unset_sentinel, blocked_message_prefix
):
    """An empty candidates list with no real block reason still degrades to
    a default empty 'stop' completion, for both dialects' own spelling of
    the "unset" sentinel."""
    del blocked_message_prefix  # only used by the sibling raises test above
    gemini_json = {
        "candidates": [],
        "promptFeedback": {"blockReason": unset_sentinel},
        "usageMetadata": {},
    }
    envelope = translate_generate_content_response_to_openai_envelope(
        gemini_json,
        model="some-model",
        dialect=dialect,
        provider_label=provider_label,
        completion_id_prefix=completion_id_prefix,
    )
    assert envelope["choices"][0]["finish_reason"] == "stop"
    assert envelope["choices"][0]["message"]["content"] == ""


def test_translate_envelope_builds_openai_shape():
    envelope = translate_generate_content_response_to_openai_envelope(
        {
            "candidates": [
                {"content": {"parts": [{"text": "Hi"}]}, "finishReason": "MAX_TOKENS"}
            ],
            "usageMetadata": {
                "promptTokenCount": 4,
                "candidatesTokenCount": 6,
                "totalTokenCount": 10,
            },
        },
        model="gemini-test",
        dialect=DEVELOPER_API_DIALECT,
        provider_label="Gemini",
        completion_id_prefix="chatcmpl-gemini-",
    )

    assert envelope["id"].startswith("chatcmpl-gemini-")
    assert envelope["model"] == "gemini-test"
    assert envelope["choices"][0]["message"]["content"] == "Hi"
    assert envelope["choices"][0]["finish_reason"] == "length"
    assert envelope["usage"]["total_tokens"] == 10


def test_translate_envelope_rejects_malformed_candidates_shape():
    with pytest.raises(GeminiTranslationError, match="Gemini candidates must be a list"):
        translate_generate_content_response_to_openai_envelope(
            {"candidates": {}, "usageMetadata": {}},
            model="gemini-test",
            dialect=DEVELOPER_API_DIALECT,
            provider_label="Gemini",
            completion_id_prefix="chatcmpl-gemini-",
        )


async def _collect_shared_sse(lines: list[str], *, dialect=DEVELOPER_API_DIALECT) -> list[object]:
    async def source() -> AsyncIterator[str]:
        for line in lines:
            yield line

    frames: list[object] = []
    async for frame in iter_openai_chat_sse_from_gemini_lines(
        source(),
        model="gemini-test",
        dialect=dialect,
        provider_label="Gemini" if dialect is DEVELOPER_API_DIALECT else "Vertex",
        completion_id_prefix="chatcmpl-gemini-",
    ):
        raw = frame[len("data: ") : -2]
        frames.append(raw if raw == "[DONE]" else json.loads(raw))
    return frames


@pytest.mark.asyncio
async def test_translate_stream_yields_text_in_order_with_final_usage_and_done():
    frames = await _collect_shared_sse(
        [
            'data: {"candidates": [{"content": {"parts": [{"text": "one "}]}}], "usageMetadata": {"promptTokenCount": 2}}',
            'data: {"candidates": [{"content": {"parts": [{"text": "two"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3}}',
            "data: [DONE]",
        ]
    )

    assert [frame["choices"][0]["delta"]["content"] for frame in frames[:2]] == ["one ", "two"]
    assert frames[0]["choices"][0]["finish_reason"] is None
    assert frames[-2]["choices"][0]["finish_reason"] == "stop"
    assert frames[-2]["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 3,
        "total_tokens": 5,
    }
    assert frames[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_translate_stream_yields_tool_call_delta_with_index():
    frames = await _collect_shared_sse(
        [
            'data: {"candidates": [{"content": {"parts": [{"functionCall": {"name": "lookup", "args": {"q": "x"}}}]}}]}',
            "data: [DONE]",
        ]
    )

    tool_call = frames[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tool_call["index"] == 0
    assert tool_call["function"]["name"] == "lookup"
    assert json.loads(tool_call["function"]["arguments"]) == {"q": "x"}
    assert frames[-2]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_translate_stream_blocked_prompt_yields_error_without_done():
    frames = await _collect_shared_sse(['data: {"promptFeedback": {"blockReason": "SAFETY"}}'])

    assert len(frames) == 1
    assert frames[0]["error"]["type"] == "provider_response_error"
    assert "Gemini generation blocked: SAFETY" in frames[0]["error"]["message"]


@pytest.mark.asyncio
async def test_translate_stream_malformed_shape_yields_error_without_done():
    frames = await _collect_shared_sse(
        ['data: {"candidates": [{"content": {"parts": ["oops"]}}]}']
    )

    assert len(frames) == 1
    assert frames[0]["error"]["type"] == "provider_response_error"
    assert "part 0 must be an object" in frames[0]["error"]["message"]


@pytest.mark.asyncio
async def test_translate_stream_ignores_malformed_json_and_non_data_lines():
    frames = await _collect_shared_sse(
        [
            "event: message",
            "data: {",
            'data: {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}',
            "data: [DONE]",
        ]
    )

    assert frames[0]["choices"][0]["delta"]["content"] == "ok"
    assert frames[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_translate_stream_rejects_unrecognized_finish_reason_without_done():
    frames = await _collect_shared_sse(
        ['data: {"candidates": [{"content": {"parts": []}, "finishReason": "OTHER"}]}']
    )

    assert len(frames) == 1
    assert frames[0]["error"]["type"] == "provider_response_error"
    assert "OTHER" in frames[0]["error"]["message"]
