from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import yaml
from anthropic.types.content_block_param import ContentBlockParam
from anthropic.types.message_create_params import MessageCreateParamsBase
from anthropic.types.raw_message_stream_event import RawMessageStreamEvent
from google.genai.types import FinishReason, GenerateContentConfig, HarmCategory, Part
from openai.types.chat.chat_completion_assistant_message_param import (
    ChatCompletionAssistantMessageParam,
)
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import Choice as ChatCompletionChunkChoice
from openai.types.chat.chat_completion_chunk import ChoiceDelta as ChatCompletionChunkDelta
from openai.types.chat.chat_completion_content_part_image_param import (
    ChatCompletionContentPartImageParam,
)
from openai.types.chat.chat_completion_content_part_input_audio_param import (
    ChatCompletionContentPartInputAudioParam,
)
from openai.types.chat.chat_completion_content_part_param import (
    File as ChatCompletionContentPartFile,
)
from openai.types.chat.chat_completion_content_part_refusal_param import (
    ChatCompletionContentPartRefusalParam,
)
from openai.types.chat.chat_completion_content_part_text_param import (
    ChatCompletionContentPartTextParam,
)
from openai.types.chat.chat_completion_custom_tool_param import ChatCompletionCustomToolParam
from openai.types.chat.chat_completion_developer_message_param import (
    ChatCompletionDeveloperMessageParam,
)
from openai.types.chat.chat_completion_function_message_param import (
    ChatCompletionFunctionMessageParam,
)
from openai.types.chat.chat_completion_function_tool_param import ChatCompletionFunctionToolParam
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam
from openai.types.chat.chat_completion_system_message_param import ChatCompletionSystemMessageParam
from openai.types.chat.chat_completion_tool_message_param import ChatCompletionToolMessageParam
from openai.types.chat.chat_completion_user_message_param import ChatCompletionUserMessageParam
from openai.types.chat.completion_create_params import CompletionCreateParamsBase
from openai.types.responses.response_create_params import ResponseCreateParamsBase
from openai.types.responses.response_input_item_param import ResponseInputItemParam
from openai.types.responses.response_stream_event import ResponseStreamEvent
from proxy.app.anthropic_compat import AnthropicMessagesRequest
from proxy.app.protocol_types import (
    OpenAIChatAssistantMessage,
    OpenAIChatChunkChoice,
    OpenAIChatChunkDelta,
    OpenAIChatCompletionChunk,
    OpenAIChatCustomTool,
    OpenAIChatDeveloperMessage,
    OpenAIChatFilePart,
    OpenAIChatFunctionMessage,
    OpenAIChatFunctionTool,
    OpenAIChatImagePart,
    OpenAIChatInputAudioPart,
    OpenAIChatRefusalPart,
    OpenAIChatRequest,
    OpenAIChatSystemMessage,
    OpenAIChatTextPart,
    OpenAIChatToolMessage,
    OpenAIChatUserMessage,
)
from proxy.app.provider_capabilities import (
    ANTHROPIC_BETA_MESSAGES_EXTENSION_FIELDS,
    ANTHROPIC_MESSAGES_FIELDS,
    CODEX_RESPONSES_EXTENSION_FIELDS,
    GEMINI_GENERATE_CONFIG_FIELDS,
    OPENAI_CHAT_FIELDS,
    OPENAI_RESPONSES_FIELDS,
    PROVIDER_CAPABILITIES,
)
from proxy.app.providers._gemini_common import SHARED_HARM_CATEGORIES
from proxy.app.responses_compat import ResponsesCreateRequest
from pydantic import BaseModel, TypeAdapter

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "client_requests"

_REQUIRED_PROVENANCE_FIELDS = ("client", "source_url", "source_commit", "captured", "description")


def _wire_field_names(model_type: type[BaseModel]) -> set[str]:
    return {field.alias or name for name, field in model_type.model_fields.items()}


def _union_type_names(annotation: object) -> set[str]:
    arguments = get_args(annotation)
    if not arguments:
        name = getattr(annotation, "__name__", None)
        return {name} if isinstance(name, str) else set()
    names: set[str] = set()
    for argument in arguments:
        if hasattr(argument, "discriminator") or isinstance(argument, str):
            continue
        names.update(_union_type_names(argument))
    return names


def _load_client_fixture(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load an immutable, provenance-labeled client request fixture.

    Fixtures live under FIXTURES_DIR as YAML files with a `provenance` map
    (source_url/source_commit/captured/description of the real client
    behavior represented) and a `request` map (the wire payload the local
    protocol DTO must round-trip). Returns (provenance, request).
    """
    path = FIXTURES_DIR / name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    provenance = document["provenance"]
    for field in _REQUIRED_PROVENANCE_FIELDS:
        value = provenance.get(field)
        assert isinstance(value, str) and value.strip(), (
            f"{name}: provenance.{field} must be a non-empty string"
        )
    return provenance, document["request"]


def test_client_request_fixtures_document_their_provenance():
    fixture_files = sorted(FIXTURES_DIR.glob("*.yaml"))
    assert fixture_files, f"no client request fixtures found under {FIXTURES_DIR}"
    for path in fixture_files:
        _load_client_fixture(path.name)


def test_openai_chat_official_top_level_drift_snapshot():
    official_fields = set(CompletionCreateParamsBase.__annotations__) | {"stream"}

    assert official_fields == OPENAI_CHAT_FIELDS
    assert _wire_field_names(OpenAIChatRequest) == official_fields


def test_openai_responses_official_top_level_drift_snapshot():
    official_fields = set(ResponseCreateParamsBase.__annotations__) | {"stream"}

    assert official_fields == OPENAI_RESPONSES_FIELDS
    assert _wire_field_names(ResponsesCreateRequest) == (
        official_fields | CODEX_RESPONSES_EXTENSION_FIELDS
    )


def test_anthropic_messages_official_top_level_drift_snapshot():
    official_fields = set(MessageCreateParamsBase.__annotations__) | {"stream"}

    assert official_fields == ANTHROPIC_MESSAGES_FIELDS
    assert _wire_field_names(AnthropicMessagesRequest) == (
        official_fields | ANTHROPIC_BETA_MESSAGES_EXTENSION_FIELDS
    )


def test_google_generate_config_official_top_level_drift_snapshot():
    assert set(GenerateContentConfig.model_fields) == GEMINI_GENERATE_CONFIG_FIELDS


def test_provider_field_classifications_only_reference_official_chat_fields():
    for capabilities in PROVIDER_CAPABILITIES.values():
        assert capabilities.chat_translation_fields <= OPENAI_CHAT_FIELDS


def test_continue_chat_contract_preserves_tools_and_stream_options():
    _, fixture = _load_client_fixture("continue_chat.yaml")

    assert OpenAIChatRequest.model_validate(fixture).to_json() == fixture


def test_hermes_chat_contract_preserves_generic_provider_tool_loop():
    _, fixture = _load_client_fixture("hermes_chat.yaml")

    assert OpenAIChatRequest.model_validate(fixture).to_json() == fixture


def test_codex_responses_contract_preserves_agent_lifecycle_items():
    _, fixture = _load_client_fixture("codex_responses.yaml")

    request = ResponsesCreateRequest.model_validate(fixture)
    assert request.model_dump(mode="json", exclude_none=True, exclude_unset=True) == fixture


def test_claude_code_messages_contract_preserves_thinking_and_tool_blocks():
    _, fixture = _load_client_fixture("claude_code_messages.yaml")

    request = AnthropicMessagesRequest.model_validate(fixture)
    assert request.model_dump(mode="json", by_alias=True, exclude_none=True) == fixture


def test_openai_chat_nested_union_drift_snapshot():
    assert _union_type_names(ChatCompletionMessageParam) == {
        "ChatCompletionAssistantMessageParam",
        "ChatCompletionDeveloperMessageParam",
        "ChatCompletionFunctionMessageParam",
        "ChatCompletionSystemMessageParam",
        "ChatCompletionToolMessageParam",
        "ChatCompletionUserMessageParam",
    }
    assert set(ChatCompletionChunk.model_fields) == {
        "choices",
        "created",
        "id",
        "model",
        "moderation",
        "object",
        "service_tier",
        "system_fingerprint",
        "usage",
    }


def test_local_openai_chat_nested_dtos_match_official_fields():
    message_pairs = [
        (OpenAIChatAssistantMessage, ChatCompletionAssistantMessageParam),
        (OpenAIChatDeveloperMessage, ChatCompletionDeveloperMessageParam),
        (OpenAIChatFunctionMessage, ChatCompletionFunctionMessageParam),
        (OpenAIChatSystemMessage, ChatCompletionSystemMessageParam),
        (OpenAIChatToolMessage, ChatCompletionToolMessageParam),
        (OpenAIChatUserMessage, ChatCompletionUserMessageParam),
    ]
    content_pairs = [
        (OpenAIChatTextPart, ChatCompletionContentPartTextParam),
        (OpenAIChatImagePart, ChatCompletionContentPartImageParam),
        (OpenAIChatInputAudioPart, ChatCompletionContentPartInputAudioParam),
        (OpenAIChatFilePart, ChatCompletionContentPartFile),
        (OpenAIChatRefusalPart, ChatCompletionContentPartRefusalParam),
    ]

    for local, official in [*message_pairs, *content_pairs]:
        assert _wire_field_names(local) == set(official.__annotations__)

    assert _wire_field_names(OpenAIChatFunctionTool) == set(
        ChatCompletionFunctionToolParam.__annotations__
    )
    assert _wire_field_names(OpenAIChatCustomTool) == set(
        ChatCompletionCustomToolParam.__annotations__
    )
    assert _wire_field_names(OpenAIChatCompletionChunk) == set(ChatCompletionChunk.model_fields)
    assert _wire_field_names(OpenAIChatChunkChoice) == set(ChatCompletionChunkChoice.model_fields)
    assert _wire_field_names(OpenAIChatChunkDelta) == set(ChatCompletionChunkDelta.model_fields)


def test_openai_responses_input_union_drift_snapshot():
    assert _union_type_names(ResponseInputItemParam) == {
        "AdditionalTools",
        "ApplyPatchCall",
        "ApplyPatchCallOutput",
        "CompactionTrigger",
        "ComputerCallOutput",
        "EasyInputMessageParam",
        "FunctionCallOutput",
        "ImageGenerationCall",
        "ItemReference",
        "LocalShellCall",
        "LocalShellCallOutput",
        "McpApprovalRequest",
        "McpApprovalResponse",
        "McpCall",
        "McpListTools",
        "Message",
        "Program",
        "ProgramOutput",
        "ResponseCodeInterpreterToolCallParam",
        "ResponseCompactionItemParamParam",
        "ResponseComputerToolCallParam",
        "ResponseCustomToolCallOutputParam",
        "ResponseCustomToolCallParam",
        "ResponseFileSearchToolCallParam",
        "ResponseFunctionToolCallParam",
        "ResponseFunctionWebSearchParam",
        "ResponseOutputMessageParam",
        "ResponseReasoningItemParam",
        "ResponseToolSearchOutputItemParamParam",
        "ShellCall",
        "ShellCallOutput",
        "ToolSearchCall",
    }


def test_openai_responses_stream_event_union_drift_snapshot():
    assert _union_type_names(ResponseStreamEvent) == {
        "ResponseAudioDeltaEvent",
        "ResponseAudioDoneEvent",
        "ResponseAudioTranscriptDeltaEvent",
        "ResponseAudioTranscriptDoneEvent",
        "ResponseCodeInterpreterCallCodeDeltaEvent",
        "ResponseCodeInterpreterCallCodeDoneEvent",
        "ResponseCodeInterpreterCallCompletedEvent",
        "ResponseCodeInterpreterCallInProgressEvent",
        "ResponseCodeInterpreterCallInterpretingEvent",
        "ResponseCompletedEvent",
        "ResponseContentPartAddedEvent",
        "ResponseContentPartDoneEvent",
        "ResponseCreatedEvent",
        "ResponseCustomToolCallInputDeltaEvent",
        "ResponseCustomToolCallInputDoneEvent",
        "ResponseErrorEvent",
        "ResponseFailedEvent",
        "ResponseFileSearchCallCompletedEvent",
        "ResponseFileSearchCallInProgressEvent",
        "ResponseFileSearchCallSearchingEvent",
        "ResponseFunctionCallArgumentsDeltaEvent",
        "ResponseFunctionCallArgumentsDoneEvent",
        "ResponseImageGenCallCompletedEvent",
        "ResponseImageGenCallGeneratingEvent",
        "ResponseImageGenCallInProgressEvent",
        "ResponseImageGenCallPartialImageEvent",
        "ResponseInProgressEvent",
        "ResponseIncompleteEvent",
        "ResponseMcpCallArgumentsDeltaEvent",
        "ResponseMcpCallArgumentsDoneEvent",
        "ResponseMcpCallCompletedEvent",
        "ResponseMcpCallFailedEvent",
        "ResponseMcpCallInProgressEvent",
        "ResponseMcpListToolsCompletedEvent",
        "ResponseMcpListToolsFailedEvent",
        "ResponseMcpListToolsInProgressEvent",
        "ResponseOutputItemAddedEvent",
        "ResponseOutputItemDoneEvent",
        "ResponseOutputTextAnnotationAddedEvent",
        "ResponseQueuedEvent",
        "ResponseReasoningSummaryPartAddedEvent",
        "ResponseReasoningSummaryPartDoneEvent",
        "ResponseReasoningSummaryTextDeltaEvent",
        "ResponseReasoningSummaryTextDoneEvent",
        "ResponseReasoningTextDeltaEvent",
        "ResponseReasoningTextDoneEvent",
        "ResponseRefusalDeltaEvent",
        "ResponseRefusalDoneEvent",
        "ResponseTextDeltaEvent",
        "ResponseTextDoneEvent",
        "ResponseWebSearchCallCompletedEvent",
        "ResponseWebSearchCallInProgressEvent",
        "ResponseWebSearchCallSearchingEvent",
    }


def test_anthropic_nested_unions_drift_snapshot():
    assert _union_type_names(ContentBlockParam) == {
        "BashCodeExecutionToolResultBlockParam",
        "CodeExecutionToolResultBlockParam",
        "ContainerUploadBlockParam",
        "DocumentBlockParam",
        "ImageBlockParam",
        "MidConversationSystemBlockParam",
        "RedactedThinkingBlockParam",
        "SearchResultBlockParam",
        "ServerToolUseBlockParam",
        "TextBlockParam",
        "TextEditorCodeExecutionToolResultBlockParam",
        "ThinkingBlockParam",
        "ToolResultBlockParam",
        "ToolSearchToolResultBlockParam",
        "ToolUseBlockParam",
        "WebFetchToolResultBlockParam",
        "WebSearchToolResultBlockParam",
    }
    assert _union_type_names(RawMessageStreamEvent) == {
        "RawContentBlockDeltaEvent",
        "RawContentBlockStartEvent",
        "RawContentBlockStopEvent",
        "RawMessageDeltaEvent",
        "RawMessageStartEvent",
        "RawMessageStopEvent",
    }


def test_google_genai_part_and_finish_reason_drift_snapshot():
    assert set(Part.model_fields) == {
        "audio_transcription",
        "code_execution_result",
        "executable_code",
        "file_data",
        "function_call",
        "function_response",
        "inline_data",
        "media_resolution",
        "part_metadata",
        "text",
        "thought",
        "thought_signature",
        "tool_call",
        "tool_response",
        "video_metadata",
    }
    assert {reason.value for reason in FinishReason} == {
        "BLOCKLIST",
        "FINISH_REASON_UNSPECIFIED",
        "IMAGE_OTHER",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
        "IMAGE_SAFETY",
        "LANGUAGE",
        "MALFORMED_FUNCTION_CALL",
        "MAX_TOKENS",
        "NO_IMAGE",
        "OTHER",
        "PROHIBITED_CONTENT",
        "RECITATION",
        "SAFETY",
        "SPII",
        "STOP",
        "TOO_MANY_TOOL_CALLS",
        "UNEXPECTED_TOOL_CALL",
    }


def test_shared_harm_categories_are_valid_official_values():
    assert {category.value for category in HarmCategory} >= SHARED_HARM_CATEGORIES


# --- Streaming lifecycle contracts -----------------------------------------
#
# The tests above validate single, static requests. Real clients also depend
# on the *ordering* of streamed chunks/events, not just their per-event
# shape (already covered by the drift-snapshot tests above). These tests
# build a realistic, ordered event sequence for each streaming protocol,
# grounded in the same client behaviors documented in the fixtures above,
# and assert both (a) every event round-trips through the relevant type
# unchanged and (b) the ordering/lifecycle invariants a real streaming
# consumer relies on. No network calls are made; every event is a static,
# hand-built dict.


def test_openai_chat_stream_defers_usage_chunk_to_final_frame():
    """Continue's chatCompletionStream() (OpenAI.ts) buffers the
    usage-carrying chunk and always emits it last, after the
    finish_reason chunk and with an empty choices list — mirroring the
    stream_options.include_usage contract asserted statically in
    continue_chat.yaml. This test builds a full tool-call turn (role
    delta -> content delta -> tool_call open -> tool_call argument delta
    -> finish_reason -> usage) and checks that ordering holds when each
    chunk round-trips through the local OpenAIChatCompletionChunk DTO.
    """
    chunks: list[dict[str, Any]] = [
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-5.6-luna",
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-5.6-luna",
            "choices": [{"index": 0, "delta": {"content": "Reading "}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-5.6-luna",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": ""},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-5.6-luna",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"path":"README.md"}'}}
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-5.6-luna",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        },
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-5.6-luna",
            "choices": [],
            "usage": {"completion_tokens": 12, "prompt_tokens": 40, "total_tokens": 52},
        },
    ]

    for chunk in chunks:
        dumped = OpenAIChatCompletionChunk.model_validate(chunk).model_dump(
            mode="json", exclude_unset=True
        )
        assert dumped == chunk

    # Lifecycle invariants a real streaming consumer relies on.
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant", "first chunk opens the role"
    finish_reason_chunks = [
        c for c in chunks if c["choices"] and c["choices"][0].get("finish_reason") is not None
    ]
    assert len(finish_reason_chunks) == 1, "finish_reason must appear exactly once"
    assert chunks.index(finish_reason_chunks[0]) == len(chunks) - 2, (
        "finish_reason chunk precedes the trailing usage chunk"
    )
    usage_chunks = [c for c in chunks if c.get("usage") is not None]
    assert len(usage_chunks) == 1, "usage must appear exactly once"
    assert chunks[-1] is usage_chunks[0], "usage chunk is deferred to the final frame"
    assert chunks[-1]["choices"] == [], "the deferred usage chunk carries no choices"


def test_openai_responses_stream_lifecycle_orders_function_call_events():
    """Codex CLI's Responses agent loop (mirroring codex_responses.yaml's
    read_file tool call) drives a strict event order: the response opens
    (created -> in_progress), a function_call output item is added, its
    arguments stream via delta events and close with a done event, the
    item itself completes, and the response closes. Each event is
    validated against the official openai-python ResponseStreamEvent
    discriminated union via TypeAdapter (it is not a BaseModel subclass,
    so model_validate is not applicable) and round-tripped unchanged.
    """
    adapter: TypeAdapter[Any] = TypeAdapter(ResponseStreamEvent)
    base_response = {
        "id": "resp_1",
        "created_at": 1700000000.0,
        "model": "gpt-5.6-codex",
        "object": "response",
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "status": "in_progress",
    }
    in_progress_call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": "",
        "status": "in_progress",
    }
    completed_call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": '{"path":"README.md"}',
        "status": "completed",
    }
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": base_response, "sequence_number": 0},
        {"type": "response.in_progress", "response": base_response, "sequence_number": 1},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": in_progress_call,
            "sequence_number": 2,
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 0,
            "delta": '{"path":"README.md"}',
            "sequence_number": 3,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "output_index": 0,
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
            "sequence_number": 4,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": completed_call,
            "sequence_number": 5,
        },
        {
            "type": "response.completed",
            "response": {**base_response, "output": [completed_call], "status": "completed"},
            "sequence_number": 6,
        },
    ]

    for event in events:
        validated = adapter.validate_python(event)
        dumped = adapter.dump_python(validated, mode="json", exclude_unset=True)
        assert dumped == event

    # Lifecycle invariants a real streaming consumer relies on.
    assert [e["type"] for e in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert events[0]["type"] == "response.created", "response.created opens the stream"
    assert events[-1]["type"] == "response.completed", "response.completed closes the stream"
    assert [e["sequence_number"] for e in events] == sorted(e["sequence_number"] for e in events), (
        "sequence_number is strictly increasing"
    )
    completed_events = [e for e in events if e["type"] == "response.completed"]
    assert len(completed_events) == 1, "response.completed must appear exactly once"
    assert completed_events[0]["response"]["output"][0]["arguments"] == '{"path":"README.md"}', (
        "the final response snapshot carries the fully-streamed arguments"
    )


def test_anthropic_messages_stream_lifecycle_orders_thinking_and_tool_use():
    """Claude Code's thinking + tool_use turn (mirroring
    claude_code_messages.yaml) opens the message, streams a thinking
    block (thinking_delta then signature_delta), closes it, opens a
    tool_use block, streams its input as input_json_delta, closes it,
    then emits a single message_delta carrying stop_reason/usage
    followed by message_stop. Each event is validated against the
    official anthropic-python RawMessageStreamEvent discriminated union
    via TypeAdapter and round-tripped unchanged.
    """
    adapter: TypeAdapter[Any] = TypeAdapter(RawMessageStreamEvent)
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-1",
                "content": [],
                "usage": {"input_tokens": 24, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "inspect repository"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "opaque-signature"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read_file",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"path":"README.md"}'},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 42},
        },
        {"type": "message_stop"},
    ]

    for event in events:
        validated = adapter.validate_python(event)
        dumped = adapter.dump_python(validated, mode="json", exclude_unset=True)
        assert dumped == event

    # Lifecycle invariants a real streaming consumer relies on.
    assert events[0]["type"] == "message_start", "message_start opens the stream"
    assert events[-1]["type"] == "message_stop", "message_stop closes the stream"
    assert [e["type"] for e in events].count("message_start") == 1
    assert [e["type"] for e in events].count("message_stop") == 1
    message_delta_events = [e for e in events if e["type"] == "message_delta"]
    assert len(message_delta_events) == 1, "message_delta (stop_reason/usage) appears exactly once"
    assert events.index(message_delta_events[0]) == len(events) - 2, (
        "message_delta immediately precedes message_stop"
    )
    assert message_delta_events[0]["delta"]["stop_reason"] == "tool_use", (
        "stop_reason reflects the tool_use turn"
    )
    block_starts = [e for e in events if e["type"] == "content_block_start"]
    block_stops = [e for e in events if e["type"] == "content_block_stop"]
    assert len(block_starts) == len(block_stops) == 2, "each content block opens and closes once"
    assert [e["index"] for e in block_starts] == [0, 1], (
        "thinking block (0) precedes tool_use block (1)"
    )
