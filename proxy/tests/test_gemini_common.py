from __future__ import annotations

import pytest
from proxy.app.providers import _gemini_common as gemini_common
from proxy.app.providers._gemini_common import (
    DEVELOPER_API_DIALECT,
    VERTEX_DIALECT,
    GeminiTranslationError,
    extract_message_text,
    is_block_reason_unset,
    translate_candidate_to_openai_choice,
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
