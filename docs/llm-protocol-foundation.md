# Typed LLM Protocol Foundation

Research snapshot: 2026-08-04

## Decision

The gateway uses a Responses-style item/event domain for stateful and agentic execution. OpenAI Chat Completions, Anthropic Messages, and Gemini GenerateContent are wire protocols at the boundary, not interchangeable internal schemas.

Rules:

1. Decode each ingress protocol into strict protocol DTOs.
2. Preserve the original validated native request.
3. Inspect and redact text leaves without deleting non-text blocks.
4. Route before translating.
5. Dispatch the original protocol when the selected provider supports it.
6. Translate only through an explicit capability-checked adapter.
7. Reject semantics an adapter cannot preserve. Never silently omit fields, blocks, calls, results, usage, or terminal errors.
8. Keep provider identity, wire protocol, response envelope, and normalized usage as separate discriminants.
9. Preserve opaque state such as reasoning encryption, Anthropic thinking signatures, tool call IDs, phases, item IDs, and provider extension fields verbatim.

## Why Chat cannot be the canonical model

Chat's `messages -> choices -> delta` model cannot represent the full lifecycle required by modern agents. Responses carries item identity, item status, reasoning items, response chaining, conversations, background state, built-in tools, tool output items, and semantic stream events.

Chat also has unique controls that are not universal: `n`, frequency and presence penalties, `logit_bias`, `stop`, `seed`, prediction, and audio behavior. These remain Chat protocol extensions and require target capability checks.

## Protocol inventory

### OpenAI Responses

Current request envelope fields observed in official `openai-python` 2.52.0:

- `model`, `input`, `instructions`
- `previous_response_id`, `conversation`, `store`, `background`
- `include`, `reasoning`, `text`
- `tools`, `tool_choice`, `parallel_tool_calls`, `max_tool_calls`
- `max_output_tokens`, `temperature`, `top_p`, `top_logprobs`, `truncation`
- `stream`, `stream_options`
- `metadata`, `safety_identifier`, `user`, `service_tier`
- `prompt`, `prompt_cache_key`, `prompt_cache_options`, `prompt_cache_retention`
- `context_management`, `moderation`

Important invariants:

- `previous_response_id` and `conversation` are mutually exclusive.
- `stream_options` requires `stream=true`.
- `input` is a string or an item list; it is not message-only history.
- Instructions are not inherited through `previous_response_id`.
- `max_output_tokens` includes visible and reasoning tokens.
- Function calls and outputs join on `call_id`, not item `id`.
- Stateless continuation must preserve reasoning items, encrypted content, item order, and assistant `phase`.

The current generated input-item union contains message, function call/output, custom tool call/output, reasoning, item references, shell/local-shell call/output, apply-patch call/output, computer call/output, MCP list/call/approval items, web/file search, code interpreter, image generation, compaction, tool search, program, and related lifecycle items.

### OpenAI Chat Completions

The request is stream-discriminated and requires `model` and `messages`.

Message roles:

- `developer`
- `system`
- `user`
- `assistant`
- `tool`
- deprecated `function`

User content includes text, image URL, input audio, and file parts. Assistant messages may contain text/refusal parts, function/custom tool calls, audio, or deprecated function calls. Tool call arguments are JSON strings and cannot be assumed valid until parsed.

Important invariants:

- Assistant content is required unless a tool/function call is present.
- `top_logprobs` requires `logprobs=true`.
- `stream_options` requires `stream=true`.
- Audio configuration is required when requesting audio output.
- Stream assembly keys are `choice.index` and then `tool_call.index`.
- A final usage-only stream chunk may have `choices=[]`.

### Anthropic Messages

Current stable request fields observed in `anthropic-sdk-python` 0.120.2:

Required:

- `model`
- `messages`
- `max_tokens`

Optional:

- `cache_control`, `container`, `inference_geo`, `metadata`
- `output_config`, `service_tier`
- `stop_sequences`, `stream`, `system`
- `temperature`, `top_k`, `top_p`
- `thinking`, `tools`, `tool_choice`
- `anthropic-user-profile-id`

Stable content block tags include:

- `text`, `image`, `document`, `search_result`
- `thinking`, `redacted_thinking`
- `tool_use`, `tool_result`, `server_tool_use`
- web search/fetch and code-execution result variants
- `tool_search_tool_result`, `container_upload`, `mid_conv_system`

Thinking configuration:

- `{type: "adaptive", display?}`
- `{type: "enabled", budget_tokens, display?}`
- `{type: "disabled"}`

Important invariants:

- Manual thinking budget is at least 1024 tokens.
- Manual thinking normally uses a budget below `max_tokens`, but interleaved-thinking beta behavior is an exception; the gateway must not overvalidate without model/header context.
- Manual thinking is incompatible with forced `any`/named-tool choice.
- Thinking and redacted-thinking blocks, signatures, and order are opaque round-trip state.
- Tool results must follow the assistant tool-use turn; tool-result blocks precede text; IDs must match.
- `is_error` is semantic data and must not be flattened into an `Error:` string.
- Stable and beta DTOs must remain separate; `anthropic-version` and `anthropic-beta` headers must survive native forwarding.

### Gemini GenerateContent

Official Google Gen AI surfaces used by an adapter include:

- `Content`, `Part`, and `GenerateContentConfig`
- function declarations under `Tool.function_declarations`
- `Part.function_call` and `Part.function_response`
- `FunctionCall.id/name/args`
- `FunctionResponse.id/name/response`
- safety, candidate, cached-content, thinking, media, schema, and usage controls

The adapter must translate these or return a capability error. Mapping unknown roles to user, stripping multipart content to text, or dropping tool messages is forbidden.

Gemini finish reasons beyond STOP/MAX_TOKENS/SAFETY include recitation, language, blocklist, prohibited content, SPII, malformed function call, unexpected tool call, and image-specific failures. They must not be reported as ordinary success.

## Dispatch and verification matrix

All verification commands below are mock-backed and make no billable provider calls. Run them from `proxy/`.

| Endpoint | Primary clients | Resolved provider | Buffered | Streaming | Contract | No-spend verification |
|---|---|---|---|---|---|---|
| `/v1/chat/completions` | Continue, Hermes, OpenAI SDK | OpenAI/open-compatible | Native JSON | Native Chat SSE | Body and supported response headers preserved | `uv --no-config run --extra dev pytest tests/test_chat.py tests/test_adapters.py -q` |
| `/v1/chat/completions` | Continue, Hermes | Anthropic | Typed Chat subset | Translated SSE | Unsupported fields/content reject before dispatch | `uv --no-config run --extra dev pytest tests/test_adapters.py -q -k anthropic` |
| `/v1/chat/completions` | Continue, Hermes | Gemini | Typed Chat subset | Translated SSE | Tools supported; unknown finish reasons fail closed | `uv --no-config run --extra dev pytest tests/test_adapters.py -q -k gemini` |
| `/v1/responses` | Codex, OpenAI SDK | OpenAI | Native JSON | Native Responses SSE | OpenAI beta headers and safe request/rate-limit headers preserved | `uv --no-config run --extra dev pytest tests/test_responses.py -q` |
| `/v1/responses` | Codex-compatible clients | Anthropic/Gemini/open-compatible | Typed subset via Chat | Text/function-call lifecycle translation | State/tool variants outside the declared subset reject | `uv --no-config run --extra dev pytest tests/test_responses.py tests/test_adapters.py -q` |
| `/v1/messages` | Claude Code, Anthropic SDK | Anthropic | Native JSON | Native Messages SSE | `anthropic-version`, `anthropic-beta`, usage, and safe response headers preserved | `uv --no-config run --extra dev pytest tests/test_messages.py -q` |
| `/v1/messages` | Claude-compatible clients | OpenAI/Gemini/open-compatible | Typed subset via Chat | Text/tool lifecycle translation | Thinking/cache/container/state semantics reject rather than flatten | `uv --no-config run --extra dev pytest tests/test_messages.py tests/test_adapters.py -q` |
| Any endpoint | Any client | Unsupported route/field combination | Explicit 4xx | No stream opened | Structured capability/field diagnostics | `uv --no-config run --extra dev pytest tests/test_official_sdk_contracts.py tests/test_properties.py -q` |

Native forwarding is the only lossless default. Cross-provider translation is a declared subset, not API equivalence.

## Internal model

`proxy/app/protocol_types.py` defines:

- recursive JSON and JSON Schema types
- a protocol-owned `GatewayPayload` boundary
- strict Chat request messages, content parts, tools, tool choices, and stream chunks
- Responses-style execution messages, function calls/results, reasoning items, item references, and semantic stream event envelopes
- the older explicit cross-provider conversation subset used while adapters migrate
- strict models with `extra="forbid"` where the gateway claims semantic understanding

Protocol DTOs may retain typed `JsonObject` extension regions where upstream unions evolve faster than the local semantic model. Those regions are forwarded only on native routes; translation adapters must reject unsupported contents.

## Current implementation guarantees

- Matching OpenAI Responses and Anthropic Messages requests bypass Chat translation.
- Native JSON and SSE are forwarded without rebuilding protocol events.
- Native Chat requests are not rewritten based on model-name heuristics.
- Safe upstream request IDs and rate-limit headers survive native forwarding; cookies and arbitrary headers do not.
- A successful non-SSE upstream response is rejected before a downstream SSE stream opens.
- OpenAI beta and Anthropic version/beta headers are forwarded on native paths.
- Responses lifecycle conflicts and Anthropic thinking/tool-choice conflicts are validated.
- Native OpenAI Responses usage accepts input/output token names.
- Structured PII redaction preserves non-text blocks and ordering.
- Flattened PII results are redistributed to their original text leaves, preserving unaffected leaves.
- Gemini function declarations, function calls, and function responses are translated; unsupported Chat controls are rejected rather than dropped.
- Anthropic function tools and named tool choices are converted to Anthropic wire shapes.

## Remaining typed migrations

These are required before claiming universal semantic compatibility:

1. Move all Anthropic content/tool/stream unions from typed JSON extension regions into discriminated DTOs.
2. Model all Responses input/output item and stream-event variants needed by Codex, including shell, patch, MCP, computer, and background lifecycle.
3. Finish strict Chat buffered-response DTOs and the remaining top-level nested request controls; request messages/content/tools and stream chunks are typed.
4. Add Gemini protocol DTOs aligned with `google.genai.types` and exhaustive finish/error mapping.
5. Normalize provider results before Starlette serialization: buffered result vs typed event stream, upstream provider, response protocol, usage, and terminal error.
6. Replace flattened PII-result redistribution with direct provider finding paths/spans when the governance backend exposes them.
7. Add captured, immutable Continue, Codex, Claude Code, and Hermes request/stream fixtures. Current client contracts are hand-authored from documented/current shapes rather than byte captures.
8. Extend the official SDK drift suite beyond top-level create fields into every nested union tag and streaming event family.

## Authoritative sources

Anthropic:

- https://platform.claude.com/docs/en/api/messages
- https://platform.claude.com/docs/en/build-with-claude/streaming
- https://platform.claude.com/docs/en/build-with-claude/thinking
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
- https://platform.claude.com/docs/en/api/versioning
- https://platform.claude.com/docs/en/api/beta-headers
- https://github.com/anthropics/anthropic-sdk-python/tree/f5c30d0490fb7bcd8e0b65d8d8e63c0e7d1bfe59/src/anthropic/types

OpenAI:

- https://developers.openai.com/api/docs/guides/responses-vs-chat-completions
- https://developers.openai.com/api/reference/resources/responses/methods/create
- https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
- https://github.com/openai/openai-python/tree/0c09a3fe815184f0a46fbf18b1aba84a467c854e/src/openai/types/responses
- https://github.com/openai/openai-python/blob/v2.52.0/src/openai/types/chat/completion_create_params.py
- https://github.com/openai/openai-python/blob/v2.52.0/src/openai/types/chat/chat_completion_chunk.py

Google and Continue:

- https://ai.google.dev/api/generate-content
- https://ai.google.dev/gemini-api/docs/function-calling
- https://googleapis.github.io/python-genai/genai.html#genai.types.GenerateContentConfig
- https://github.com/googleapis/python-genai/blob/a0faf87f3b88b14bb684c8fde60fc7c08c0e6d59/google/genai/types.py
- https://github.com/continuedev/continue/blob/5522c6f44ca0ac3528b37244818fbfa39b5af470/packages/openai-adapters/src/apis/OpenAI.ts
- https://github.com/continuedev/continue/blob/5522c6f44ca0ac3528b37244818fbfa39b5af470/packages/openai-adapters/src/apis/Anthropic.ts

Client implementations and contracts:

- Codex: https://github.com/openai/codex/tree/5d89ab65dc9d4d0c55796c11df112b54157922b4
- Claude Code API configuration: https://docs.anthropic.com/en/docs/claude-code/llm-gateway
- Hermes Agent: https://github.com/NousResearch/hermes-agent/tree/aec331899e4748739927fddf02a54327e64419a0
