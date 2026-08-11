"""Shared Gemini request/response translation core.

Used by both proxy/app/providers/gemini.py (API-key, Developer API) and
proxy/app/providers/gemini_vertex.py (SA-authenticated, Vertex AI).

Only the fields/behaviors that are identical (or trivially parameterizable)
across both backends live here. Backend-specific divergences (see
docs/spec-gemini-vertex-adapter.md, "Body-shape divergences") stay in each
adapter module.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import cast

from proxy.app.protocol_types import JsonObject


class GeminiTranslationError(ValueError):
    """Chat semantics cannot be represented by Gemini without loss."""


@dataclass(frozen=True)
class GeminiDialect:
    """Per-backend knobs for the shared translation functions."""

    name: str  # "gemini" or "gemini-vertex"
    include_model_in_body: bool  # True for Developer API, False for Vertex
    block_reason_unset: str  # "BLOCK_REASON_UNSPECIFIED" | "BLOCKED_REASON_UNSPECIFIED"
    extra_finish_reasons: frozenset[str] = field(default_factory=frozenset)
    # Vertex-only: {"MODEL_ARMOR"}
    # Developer-API-only: {"LANGUAGE", "TOO_MANY_TOOL_CALLS",
    #                       "MISSING_THOUGHT_SIGNATURE", "MALFORMED_RESPONSE",
    #                       "ESCALATION"}


DEVELOPER_API_DIALECT = GeminiDialect(
    name="gemini",
    include_model_in_body=True,
    block_reason_unset="BLOCK_REASON_UNSPECIFIED",
    extra_finish_reasons=frozenset(
        {"LANGUAGE", "TOO_MANY_TOOL_CALLS", "MISSING_THOUGHT_SIGNATURE",
         "MALFORMED_RESPONSE", "ESCALATION"}
    ),
)

VERTEX_DIALECT = GeminiDialect(
    name="gemini-vertex",
    include_model_in_body=False,
    block_reason_unset="BLOCKED_REASON_UNSPECIFIED",
    extra_finish_reasons=frozenset({"MODEL_ARMOR"}),
)

# HarmCategory values present on BOTH backends. Adapters may pass through
# additional backend-specific categories, but only these four are asserted
# in shared contract tests.
SHARED_HARM_CATEGORIES = frozenset(
    {"HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_DANGEROUS_CONTENT",
     "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_SEXUALLY_EXPLICIT"}
)

FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
}


def extract_message_text(content: object, *, location: str) -> str:
    """Extract a string from an OpenAI `content` field (string, None, or a
    list of {"type": "text"|"input_text", "text": ...} parts)."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        raise GeminiTranslationError(f"{location} content must be text")
    text: list[str] = []
    for index, part in enumerate(content):
        if not isinstance(part, dict):
            raise GeminiTranslationError(
                f"{location} content part {index} is not supported by the Gemini adapter"
            )
        part_object = cast(dict[str, object], part)
        if part_object.get("type") not in {"text", "input_text"}:
            raise GeminiTranslationError(
                f"{location} content part {index} is not supported by the Gemini adapter"
            )
        value = part_object.get("text")
        if not isinstance(value, str):
            raise GeminiTranslationError(f"{location} content part {index} requires string text")
        text.append(value)
    return "".join(text)


def translate_openai_messages_to_contents(messages: list[JsonObject]) -> list[JsonObject]:
    """OpenAI-style messages -> Gemini `contents` (Content/Part list).

    Identical shape on both backends. `system`/`developer` messages are
    validated (their text is extracted) but do not produce a `contents`
    entry — callers that need the system instruction re-derive it
    separately with `extract_message_text`.
    """
    contents: list[JsonObject] = []
    tool_names_by_call_id: dict[str, str] = {}

    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise GeminiTranslationError(f"message {message_index} must be an object")
        role = message.get("role")
        if not isinstance(role, str):
            raise GeminiTranslationError(f"message {message_index} requires a role")
        text = extract_message_text(message.get("content"), location=f"message {message_index}")

        if role in {"system", "developer"}:
            continue
        elif role == "assistant":
            parts: list[JsonObject] = []
            if text:
                parts.append({"text": text})
            tool_calls = message.get("tool_calls", [])
            if not isinstance(tool_calls, list):
                raise GeminiTranslationError(f"message {message_index} tool_calls must be a list")
            for call_index, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, dict) or tool_call.get("type", "function") != "function":
                    raise GeminiTranslationError(
                        f"message {message_index} tool call {call_index} must be a function call"
                    )
                function = tool_call.get("function")
                if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                    raise GeminiTranslationError(
                        f"message {message_index} tool call {call_index} requires a function name"
                    )
                raw_arguments = function.get("arguments", "{}")
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except json.JSONDecodeError as exc:
                    raise GeminiTranslationError(
                        f"message {message_index} tool call {call_index} arguments are not valid JSON"
                    ) from exc
                if not isinstance(arguments, dict):
                    raise GeminiTranslationError(
                        f"message {message_index} tool call {call_index} arguments must be an object"
                    )
                call_id = tool_call.get("id")
                name = function["name"]
                function_call: JsonObject = {"name": name, "args": arguments}
                if isinstance(call_id, str):
                    function_call["id"] = call_id
                    tool_names_by_call_id[call_id] = name
                parts.append({"functionCall": function_call})
            contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in tool_names_by_call_id:
                raise GeminiTranslationError(
                    f"message {message_index} tool_call_id does not reference a prior call"
                )
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "id": call_id,
                                "name": tool_names_by_call_id[call_id],
                                "response": {"output": text},
                            }
                        }
                    ],
                }
            )
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": text}]})
        else:
            raise GeminiTranslationError(f"message {message_index} has unsupported role {role!r}")

    return contents


def translate_generation_config(openai_request: JsonObject) -> JsonObject:
    """OpenAI sampling params -> Gemini `generationConfig`.

    Identical field names on both backends. Returns {} when nothing is set;
    callers decide whether to attach the key.
    """
    generation_config: JsonObject = {}
    if "temperature" in openai_request:
        generation_config["temperature"] = openai_request["temperature"]
    if "top_p" in openai_request:
        generation_config["topP"] = openai_request["top_p"]
    if "max_completion_tokens" in openai_request or "max_tokens" in openai_request:
        generation_config["maxOutputTokens"] = openai_request.get(
            "max_completion_tokens", openai_request.get("max_tokens")
        )
    if "stop" in openai_request:
        stop = openai_request["stop"]
        generation_config["stopSequences"] = [stop] if isinstance(stop, str) else stop
    return generation_config


def translate_tools(openai_tools: object) -> list[JsonObject] | None:
    """OpenAI `tools` function declarations -> Gemini `tools`.

    Identical shape on both backends. Does not translate `tool_choice` ->
    `toolConfig` (a separate top-level field); that stays in each adapter.
    """
    if openai_tools is None:
        return None
    if not isinstance(openai_tools, list):
        raise GeminiTranslationError("tools must be a list")
    declarations: list[JsonObject] = []
    for index, tool in enumerate(openai_tools):
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise GeminiTranslationError(f"tool {index} is not a function tool")
        function = tool.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise GeminiTranslationError(f"tool {index} requires a function definition")
        declaration: JsonObject = {"name": function["name"]}
        if "description" in function:
            declaration["description"] = function["description"]
        if "parameters" in function:
            declaration["parameters"] = function["parameters"]
        declarations.append(declaration)
    return [{"functionDeclarations": declarations}]


def translate_tool_choice(tool_choice: object) -> JsonObject | None:
    """OpenAI `tool_choice` -> Gemini `toolConfig`.

    Identical semantics on both backends (only named-function forcing,
    "auto", "none", and "required" have a proven Gemini equivalent via
    functionCallingConfig.mode). Returns None when tool_choice is unset;
    callers decide whether to attach the `toolConfig` key. Raises
    GeminiTranslationError rather than silently dropping the caller's
    tool-selection intent for any shape without a proven equivalent.
    """
    if tool_choice is None:
        return None
    config: JsonObject
    if tool_choice == "auto":
        config = {"mode": "AUTO"}
    elif tool_choice == "none":
        config = {"mode": "NONE"}
    elif tool_choice == "required":
        config = {"mode": "ANY"}
    elif isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        if tool_choice.get("type") != "function" or not isinstance(function, dict):
            raise GeminiTranslationError("Gemini adapter only supports named function tool_choice")
        name = function.get("name")
        if not isinstance(name, str):
            raise GeminiTranslationError("named function tool_choice requires a name")
        config = {"mode": "ANY", "allowedFunctionNames": [name]}
    else:
        raise GeminiTranslationError("unsupported Gemini tool_choice")
    return {"functionCallingConfig": config}


def translate_chat_request(
    body: JsonObject,
    *,
    allowed_fields: frozenset[str],
    default_model: str | None = None,
) -> tuple[str, JsonObject]:
    """OpenAI Chat body -> Gemini/Vertex generateContent body, or fail before
    losing request semantics.

    Shared by gemini.py's `_translate_request` and gemini_vertex.py's
    `_translate_chat_body` so both adapters reject the same unsupported Chat
    fields and translate tool_choice identically rather than silently
    dropping it. `allowed_fields` is passed in (rather than imported here)
    to avoid a circular import with provider_capabilities.py, which itself
    imports this module.

    `default_model` controls the two adapters' differing model-resolution
    semantics: gemini.py falls back to a default model name when the `model`
    key is absent, while gemini_vertex.py (default_model=None) requires the
    caller to supply a non-empty model string.
    """
    unsupported = sorted(
        key for key, value in body.items() if key not in allowed_fields and value is not None
    )
    if unsupported:
        raise GeminiTranslationError(
            "Gemini adapter does not support Chat fields: " + ", ".join(unsupported)
        )

    if default_model is not None:
        model_value = body.get("model", default_model)
        if not isinstance(model_value, str):
            raise GeminiTranslationError("model must be a string")
    else:
        model_value = body.get("model")
        if not isinstance(model_value, str) or not model_value:
            raise GeminiTranslationError("model must be a string")

    messages = body.get("messages", [])
    if not isinstance(messages, list):
        raise GeminiTranslationError("messages must be a list")

    contents = translate_openai_messages_to_contents(cast("list[JsonObject]", messages))

    system_parts = [
        extract_message_text(message.get("content"), location=f"message {message_index}")
        for message_index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") in {"system", "developer"}
    ]

    gemini_body: JsonObject = {"contents": contents}
    if system_parts:
        gemini_body["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}

    generation_config = translate_generation_config(body)
    if generation_config:
        gemini_body["generationConfig"] = generation_config

    tools = translate_tools(body.get("tools"))
    if tools is not None:
        gemini_body["tools"] = tools

    tool_config = translate_tool_choice(body.get("tool_choice"))
    if tool_config is not None:
        gemini_body["toolConfig"] = tool_config

    return model_value, gemini_body


def translate_candidate_to_openai_choice(
    candidate: JsonObject, dialect: GeminiDialect, index: int
) -> JsonObject:
    """Gemini `candidates[i]` -> OpenAI `choices[i]`.

    Uses dialect.extra_finish_reasons only to avoid raising on a
    backend-legitimate finishReason value the other backend doesn't define;
    the OpenAI-facing finish_reason mapping itself is shared.
    """
    raw_content = candidate.get("content", {})
    if not isinstance(raw_content, dict):
        raise GeminiTranslationError("Gemini candidate content must be an object")
    content_obj = cast(JsonObject, raw_content)
    raw_parts = content_obj.get("parts", [])
    if not isinstance(raw_parts, list):
        raise GeminiTranslationError("Gemini candidate parts must be a list")

    text_parts: list[str] = []
    tool_calls: list[JsonObject] = []
    for part_index, raw_part in enumerate(raw_parts):
        if not isinstance(raw_part, dict):
            raise GeminiTranslationError(f"Gemini candidate part {part_index} must be an object")
        part = cast(JsonObject, raw_part)
        if "text" in part:
            text = part["text"]
            if not isinstance(text, str):
                raise GeminiTranslationError(
                    f"Gemini candidate part {part_index} text must be a string"
                )
            text_parts.append(text)
        raw_function_call = part.get("functionCall")
        if isinstance(raw_function_call, dict):
            function_call = cast(JsonObject, raw_function_call)
            name = function_call.get("name")
            if not isinstance(name, str) or not name:
                raise GeminiTranslationError(
                    f"Gemini functionCall in part {part_index} must include a name"
                )
            tool_calls.append(
                {
                    "id": function_call.get("id", f"call_gemini_{secrets.token_hex(8)}"),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(function_call.get("args", {})),
                    },
                }
            )

    gemini_finish = candidate.get("finishReason", "STOP")
    if not isinstance(gemini_finish, str):
        raise GeminiTranslationError("Gemini finishReason must be a string")
    if gemini_finish in FINISH_REASON_MAP:
        finish_reason = FINISH_REASON_MAP[gemini_finish]
    elif gemini_finish in dialect.extra_finish_reasons:
        # Backend-legitimate but dialect-specific reason. Treated the same
        # as SAFETY: a policy/malformed-output stop rather than a normal
        # completion.
        finish_reason = "content_filter"
    else:
        raise GeminiTranslationError(f"Gemini generation failed: {gemini_finish}")

    message: JsonObject = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"

    return {
        "index": index,
        "message": message,
        "finish_reason": finish_reason,
    }


def translate_usage_metadata(usage_metadata: JsonObject) -> JsonObject:
    """Gemini `usageMetadata` -> OpenAI `usage`.

    Field names are identical on both backends; no dialect parameter needed.
    """
    return {
        "prompt_tokens": usage_metadata.get("promptTokenCount", 0),
        "completion_tokens": usage_metadata.get("candidatesTokenCount", 0),
        "total_tokens": usage_metadata.get("totalTokenCount", 0),
    }


def is_block_reason_unset(block_reason: str | None, dialect: GeminiDialect) -> bool:
    """Handles the BLOCK_REASON_UNSPECIFIED vs BLOCKED_REASON_UNSPECIFIED
    sentinel spelling difference between the two backends."""
    return block_reason is None or block_reason == dialect.block_reason_unset


def extract_block_reason(source: JsonObject, dialect: GeminiDialect) -> str | None:
    """Extract `promptFeedback.blockReason` from a Gemini/Vertex response or
    stream chunk, returning None when absent or equal to the dialect's
    "unset" sentinel value (i.e. the prompt was not actually blocked)."""
    raw_prompt_feedback = source.get("promptFeedback")
    block_reason = (
        raw_prompt_feedback.get("blockReason") if isinstance(raw_prompt_feedback, dict) else None
    )
    return None if is_block_reason_unset(block_reason, dialect) else block_reason


def raise_if_prompt_blocked(source: JsonObject, dialect: GeminiDialect, *, provider_label: str) -> None:
    """Raise GeminiTranslationError if `source`'s promptFeedback.blockReason
    indicates the generation was blocked."""
    block_reason = extract_block_reason(source, dialect)
    if block_reason is not None:
        raise GeminiTranslationError(f"{provider_label} generation blocked: {block_reason}")
