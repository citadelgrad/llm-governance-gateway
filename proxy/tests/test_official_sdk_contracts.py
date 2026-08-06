from __future__ import annotations

from typing import get_args

from anthropic.types.content_block_param import ContentBlockParam
from anthropic.types.message_create_params import MessageCreateParamsBase
from anthropic.types.raw_message_stream_event import RawMessageStreamEvent
from google.genai.types import FinishReason, GenerateContentConfig, Part
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
from proxy.app.responses_compat import ResponsesCreateRequest
from pydantic import BaseModel


def _wire_field_names(model_type: type[BaseModel]) -> set[str]:
    return {
        field.alias or name
        for name, field in model_type.model_fields.items()
    }


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
    fixture = {
        "model": "gpt-5.6-luna",
        "messages": [
            {"role": "system", "content": "You are a coding assistant."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect this screenshot"},
                    {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    assert OpenAIChatRequest.model_validate(fixture).to_json() == fixture


def test_codex_responses_contract_preserves_agent_lifecycle_items():
    fixture = {
        "model": "gpt-5.6-codex",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": "{\"path\":\"README.md\"}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "contents",
            },
        ],
        "previous_response_id": "resp_previous",
        "reasoning": {"effort": "high", "summary": "concise"},
        "client_metadata": {"originator": "codex_cli_rs", "client_version": "0.145.0"},
        "stream": True,
    }

    request = ResponsesCreateRequest.model_validate(fixture)
    assert request.model_dump(mode="json", exclude_none=True, exclude_unset=True) == fixture


def test_claude_code_messages_contract_preserves_thinking_and_tool_blocks():
    fixture = {
        "model": "claude-opus-4-1",
        "max_tokens": 4096,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "inspect repository",
                        "signature": "opaque-signature",
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "contents",
                    }
                ],
            },
        ],
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "context_management": {
            "edits": [{"type": "clear_thinking_20251015", "keep": "all"}]
        },
        "tools": [
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {"type": "object"},
            }
        ],
        "stream": True,
    }

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
        "code_execution_result",
        "executable_code",
        "file_data",
        "function_call",
        "function_response",
        "inline_data",
        "media_resolution",
        "text",
        "thought",
        "thought_signature",
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
        "UNEXPECTED_TOOL_CALL",
    }
