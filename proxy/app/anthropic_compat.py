"""Anthropic Messages API compatibility types and translation helpers for Claude Code support."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict


class AnthropicCompatError(Exception):
    """Raised when a request uses an unsupported Anthropic Messages shape."""


class AnthropicMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str | list[dict[str, Any]]


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[AnthropicMessage]
    system: str | list[dict[str, Any]] | None = None
    max_tokens: int = 1024
    temperature: float | None = None
    stream: bool = False
    top_p: float | None = None
    stop_sequences: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | str | None = None
    metadata: dict[str, Any] | None = None


class CountTokensRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[AnthropicMessage]
    system: str | list[dict[str, Any]] | None = None


def _tool_result_content_str(content: Any) -> str:
    """Flatten a tool_result block's `content` (str or list of content blocks) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        fragments: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                fragments.append(block.get("text", ""))
            elif isinstance(block, dict):
                fragments.append(json.dumps(block))
            else:
                fragments.append(str(block))
        return "".join(fragments)
    return json.dumps(content)


def _content_str(content: str | list[dict[str, Any]]) -> str:
    """Best-effort flattening of Anthropic message/system content to plain text.

    Used for the system prompt and for token-count approximation, both of which only
    need a representative string rather than a structural translation. tool_use/
    tool_result blocks are approximated by their JSON/text payload rather than
    rejected, so count_tokens keeps working on conversations that include them.
    """
    if isinstance(content, str):
        return content

    fragments: list[str] = []
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            raise AnthropicCompatError(f"Unsupported content block at index {index}")
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise AnthropicCompatError("Text content blocks must include string text")
            fragments.append(text)
        elif block_type == "tool_use":
            fragments.append(json.dumps(block.get("input", {})))
        elif block_type == "tool_result":
            fragments.append(_tool_result_content_str(block.get("content")))
        else:
            raise AnthropicCompatError(f"Unsupported content block type: {block_type}")
    return "".join(fragments)


def _tool_result_to_message(block: dict[str, Any], index: int) -> dict:
    """Translate an Anthropic tool_result block to an OpenAI role:"tool" message.

    OpenAI tool messages have no dedicated error flag, so an `is_error: true` result
    is represented by prefixing the flattened content with "Error: " — the same
    convention used by OpenAI-ecosystem agent frameworks (e.g. LangChain's
    ToolMessage) to surface tool failures to the model without a separate field.
    """
    tool_use_id = block.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise AnthropicCompatError(f"tool_result block at index {index} is missing tool_use_id")
    text = _tool_result_content_str(block.get("content"))
    if block.get("is_error"):
        text = f"Error: {text}" if text else "Error"
    return {"role": "tool", "tool_call_id": tool_use_id, "content": text}


def _expand_user_content_blocks(content: list[dict[str, Any]], index: int) -> list[dict]:
    """Translate a user message's content blocks into one or more OpenAI messages.

    Contiguous text blocks become a single user message; each tool_result block
    becomes its own role:"tool" message, in the same order they appear so a
    conversation with interleaved text/tool_result blocks preserves ordering.
    """
    messages: list[dict] = []
    text_buffer: list[str] = []

    def _flush_text() -> None:
        if text_buffer:
            messages.append({"role": "user", "content": "".join(text_buffer)})
            text_buffer.clear()

    for block in content:
        if not isinstance(block, dict):
            raise AnthropicCompatError(f"Unsupported content block at index {index}")
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise AnthropicCompatError("Text content blocks must include string text")
            text_buffer.append(text)
        elif block_type == "tool_result":
            _flush_text()
            messages.append(_tool_result_to_message(block, index))
        else:
            raise AnthropicCompatError(f"Unsupported content block type: {block_type}")

    _flush_text()
    if not messages:
        messages.append({"role": "user", "content": ""})
    return messages


def _assistant_content_to_message(content: list[dict[str, Any]], index: int) -> dict:
    """Translate an assistant message's content blocks (text + tool_use) to OpenAI shape."""
    text_fragments: list[str] = []
    tool_calls: list[dict] = []

    for block in content:
        if not isinstance(block, dict):
            raise AnthropicCompatError(f"Unsupported content block at index {index}")
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise AnthropicCompatError("Text content blocks must include string text")
            text_fragments.append(text)
        elif block_type == "tool_use":
            tool_id = block.get("id")
            name = block.get("name")
            if not isinstance(tool_id, str) or not tool_id:
                raise AnthropicCompatError(f"tool_use block at index {index} is missing id")
            if not isinstance(name, str) or not name:
                raise AnthropicCompatError(f"tool_use block at index {index} is missing name")
            tool_calls.append(
                {
                    "id": tool_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(block.get("input", {}))},
                }
            )
        else:
            raise AnthropicCompatError(f"Unsupported content block type: {block_type}")

    message: dict = {"role": "assistant", "content": "".join(text_fragments)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic tool definitions to OpenAI function-tool schema."""
    openai_tools: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise AnthropicCompatError(f"Unsupported tool definition at index {index}")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise AnthropicCompatError(f"Tool definition at index {index} is missing a name")
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return openai_tools


def _tool_choice_to_openai(tool_choice: dict[str, Any] | str) -> Any:
    """Translate Anthropic tool_choice to its OpenAI chat-completions equivalent.

    Anthropic shapes: {"type": "auto"}, {"type": "any"}, {"type": "none"},
    {"type": "tool", "name": "X"} (also accepted as a bare shortcut string).
    """
    if isinstance(tool_choice, str):
        tool_choice = {"type": tool_choice}
    if not isinstance(tool_choice, dict):
        raise AnthropicCompatError("Unsupported tool_choice shape")

    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        name = tool_choice.get("name")
        if not isinstance(name, str) or not name:
            raise AnthropicCompatError("tool_choice of type 'tool' must include a name")
        return {"type": "function", "function": {"name": name}}
    raise AnthropicCompatError(f"Unsupported tool_choice type: {choice_type}")


def messages_to_chat_body(req: AnthropicMessagesRequest) -> dict:
    """Translate Anthropic Messages request to internal chat completion body."""
    msgs: list[dict] = []
    if req.system:
        msgs.append({"role": "system", "content": _content_str(req.system)})
    for index, msg in enumerate(req.messages):
        role = msg.role.lower()
        if role not in {"user", "assistant"}:
            raise AnthropicCompatError(f"Unsupported message role at index {index}: {msg.role}")
        content = msg.content
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
        elif role == "assistant":
            msgs.append(_assistant_content_to_message(content, index))
        else:
            msgs.extend(_expand_user_content_blocks(content, index))

    body: dict = {
        "model": req.model,
        "messages": msgs,
        "max_tokens": req.max_tokens,
        "stream": req.stream,
    }
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.top_p is not None:
        body["top_p"] = req.top_p
    if req.stop_sequences:
        body["stop"] = req.stop_sequences
    if req.tools:
        body["tools"] = _tools_to_openai(req.tools)
    if req.tool_choice is not None:
        body["tool_choice"] = _tool_choice_to_openai(req.tool_choice)
    return body


_FINISH_TO_STOP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}


def _tool_calls_to_blocks(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for call in tool_calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            continue
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError:
            arguments = {"arguments": raw_arguments}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or "toolu_compat",
                "name": function.get("name") or "unknown_tool",
                "input": arguments if isinstance(arguments, dict) else {"value": arguments},
            }
        )
    return blocks


def chat_response_to_anthropic(chat_json: dict, model: str) -> dict:
    """Translate OpenAI chat completion JSON to Anthropic Messages API response shape."""
    choices = chat_json.get("choices") or [{}]
    choice = choices[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")
    stop_reason = _FINISH_TO_STOP.get(finish_reason, "end_turn")
    usage = chat_json.get("usage", {})

    content: list[dict[str, Any]] = []
    text = message.get("content") or ""
    if text:
        content.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls") or []
    if isinstance(tool_calls, list):
        content.extend(_tool_calls_to_blocks(tool_calls))
    if not content:
        content.append({"type": "text", "text": ""})

    return {
        "id": chat_json.get("id", "msg_compat"),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": chat_json.get("model", model),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def _iter_sse_json(body_iterator):
    """Parse `data:` lines out of a raw SSE byte/str iterator as JSON payloads.

    Buffers text across chunk boundaries so a `data: {...}` line split across two
    network reads (fragmented SSE) is reassembled before being parsed, instead of
    silently failing json.loads on a half-line and dropping the event.
    """
    buffer = ""
    async for raw in body_iterator:
        buffer += raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                yield json.loads(data_str)
            except json.JSONDecodeError:
                continue

    # Upstream may close without a trailing newline after the last data: line.
    line = buffer.rstrip("\r")
    if line.startswith("data:"):
        data_str = line[len("data:"):].strip()
        if data_str and data_str != "[DONE]":
            try:
                yield json.loads(data_str)
            except json.JSONDecodeError:
                pass


def _message_start_event(message_id: str, model: str) -> str:
    _start = {
        "type": "message_start",
        "message": {
            "id": message_id, "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    return f"event: message_start\ndata: {json.dumps(_start)}\n\n"


def _content_block_start_text_event() -> str:
    _cb_start = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    return f"event: content_block_start\ndata: {json.dumps(_cb_start)}\n\n"


def _content_block_stop_event(index: int) -> str:
    return f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': index})}\n\n"


def _message_delta_event(stop_reason: str, output_tokens: int) -> str:
    _msg_delta = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }
    return f"event: message_delta\ndata: {json.dumps(_msg_delta)}\n\n"


def _message_stop_event() -> str:
    return f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"


async def openai_sse_to_anthropic_sse(body_iterator, model: str):
    """Translate OpenAI SSE stream chunks to Anthropic Messages SSE events.

    Text deltas stream on content block index 0 (opened eagerly on the first event,
    whose id is reused as the message id so the client sees the real provider-assigned
    id instead of a fabricated placeholder). Tool-call deltas are tracked per OpenAI
    `tool_calls[].index` and translated into their own Anthropic content blocks
    (index 1, 2, ...): a content_block_start carrying the tool id/name the first time
    an index is seen, then input_json_delta events for each `arguments` fragment, with
    all open blocks closed out once a finish_reason arrives. A bare `{"error": ...}`
    event (emitted by provider adapters on upstream timeout/connection failure) is
    surfaced as an Anthropic `error` SSE event instead of being silently dropped.
    """
    message_id = "msg_compat"
    message_started = False
    open_block_indices: list[int] = []
    tool_index_by_openai_index: dict[int, int] = {}
    next_block_index = 1  # index 0 is reserved for the text block
    output_tokens = 0

    async for event in _iter_sse_json(body_iterator):
        if not message_started:
            message_id = event.get("id") or message_id
            yield _message_start_event(message_id, model)
            yield _content_block_start_text_event()
            open_block_indices.append(0)
            message_started = True

        choices = event.get("choices") or []
        raw_error = event.get("error")
        if raw_error is not None and not choices:
            error_info = raw_error if isinstance(raw_error, dict) else {"message": str(raw_error)}
            _error = {
                "type": "error",
                "error": {
                    "type": error_info.get("type", "api_error"),
                    "message": error_info.get("message", "Upstream provider error"),
                },
            }
            yield f"event: error\ndata: {json.dumps(_error)}\n\n"
            return

        usage = event.get("usage")
        if isinstance(usage, dict) and "completion_tokens" in usage:
            output_tokens = usage["completion_tokens"]

        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        finish_reason = choices[0].get("finish_reason")

        if delta.get("content"):
            _delta = {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": delta["content"]},
            }
            yield f"event: content_block_delta\ndata: {json.dumps(_delta)}\n\n"

        tool_call_deltas = delta.get("tool_calls")
        if isinstance(tool_call_deltas, list):
            for tool_call in tool_call_deltas:
                if not isinstance(tool_call, dict):
                    continue
                openai_index = tool_call.get("index", 0)
                function = tool_call.get("function") or {}
                if openai_index not in tool_index_by_openai_index:
                    block_index = next_block_index
                    next_block_index += 1
                    tool_index_by_openai_index[openai_index] = block_index
                    _cb_start = {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_call.get("id") or f"toolu_compat_{block_index}",
                            "name": function.get("name") or "",
                            "input": {},
                        },
                    }
                    yield f"event: content_block_start\ndata: {json.dumps(_cb_start)}\n\n"
                    open_block_indices.append(block_index)
                else:
                    block_index = tool_index_by_openai_index[openai_index]

                arguments_fragment = function.get("arguments")
                if arguments_fragment:
                    _delta = {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "input_json_delta", "partial_json": arguments_fragment},
                    }
                    yield f"event: content_block_delta\ndata: {json.dumps(_delta)}\n\n"

        if finish_reason:
            stop_reason = _FINISH_TO_STOP.get(finish_reason, "end_turn")
            for index in open_block_indices:
                yield _content_block_stop_event(index)
            open_block_indices.clear()
            yield _message_delta_event(stop_reason, output_tokens)
            yield _message_stop_event()
            return

    if not message_started:
        # Upstream closed without emitting a single parseable event.
        yield _message_start_event(message_id, model)
        yield _content_block_start_text_event()
        open_block_indices.append(0)

    if open_block_indices:
        for index in open_block_indices:
            yield _content_block_stop_event(index)
        yield _message_delta_event("end_turn", output_tokens)
        yield _message_stop_event()


def count_tokens_approximate(
    messages: list[AnthropicMessage], system: str | list[dict[str, Any]] | None = None
) -> int:
    """Deterministic token approximation (~4 chars per token, plus per-message overhead)."""
    total_chars = len(_content_str(system)) if system else 0
    for msg in messages:
        total_chars += len(_content_str(msg.content))
    return max(1, (total_chars + 3) // 4 + len(messages) * 5)
