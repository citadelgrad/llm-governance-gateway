from __future__ import annotations

import pytest
from proxy.app.providers import _gemini_common as gemini_common
from proxy.app.providers import gemini as gemini_provider
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
from proxy.app.providers.gemini_vertex import _to_openai_envelope as vertex_to_openai_envelope

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
# _to_openai_envelope blockReason wiring — shared across both Gemini adapters
# ---------------------------------------------------------------------------
#
# gemini.py's and gemini_vertex.py's _to_openai_envelope both delegate their
# candidates-less-response handling to raise_if_prompt_blocked /
# extract_block_reason above. Previously each adapter's own test file
# re-implemented near-identical raises-or-defaults-to-stop assertions
# separately (test_adapters.py and test_gemini_vertex_adapter.py); this
# parametrized pair covers both dialects in one place instead.

_BLOCKED_PROMPT_ADAPTER_CASES = [
    pytest.param(
        gemini_provider._to_openai_envelope,
        "BLOCK_REASON_UNSPECIFIED",
        "Gemini generation blocked",
        id="developer-api",
    ),
    pytest.param(
        vertex_to_openai_envelope,
        "BLOCKED_REASON_UNSPECIFIED",
        "Vertex generation blocked",
        id="vertex",
    ),
]


@pytest.mark.parametrize(
    ("to_openai_envelope", "unset_sentinel", "blocked_message_prefix"),
    _BLOCKED_PROMPT_ADAPTER_CASES,
)
def test_to_openai_envelope_raises_on_blocked_prompt(
    to_openai_envelope, unset_sentinel, blocked_message_prefix
):
    """An empty candidates list with a genuine promptFeedback.blockReason
    must surface as an error, not a silent empty 'stop' completion — for
    both the Gemini Developer API and Vertex dialects."""
    del unset_sentinel  # only used by the sibling default-stop test below
    gemini_json = {
        "candidates": [],
        "promptFeedback": {"blockReason": "SAFETY"},
        "usageMetadata": {},
    }
    with pytest.raises(GeminiTranslationError, match=f"{blocked_message_prefix}: SAFETY"):
        to_openai_envelope(gemini_json, "some-model")


@pytest.mark.parametrize(
    ("to_openai_envelope", "unset_sentinel", "blocked_message_prefix"),
    _BLOCKED_PROMPT_ADAPTER_CASES,
)
def test_to_openai_envelope_default_stop_when_block_reason_unset(
    to_openai_envelope, unset_sentinel, blocked_message_prefix
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
    envelope = to_openai_envelope(gemini_json, "some-model")
    assert envelope["choices"][0]["finish_reason"] == "stop"
    assert envelope["choices"][0]["message"]["content"] == ""
