# Test Results: ai-gateway-7yl0.6

## Task

Deepen client wire codecs and thin protocol endpoints.

## Run

- Timestamp: 2026-08-11
- Focused: `cd proxy && uv --no-config run --frozen --extra dev python -m pytest tests/test_client_codecs.py tests/test_provider_dispatch.py tests/test_responses.py tests/test_messages.py tests/test_chat.py tests/test_official_sdk_contracts.py tests/test_openai_chat_stream.py tests/test_stream_events.py tests/test_stream_usage.py tests/test_protocol_types.py`
- Full suite: `make test`
- Lint/type gate: `make lint`
- Result: PASS

## Suites

- Focused client/protocol suite: 171 passed in 1.65s
- `make test`:
  - Proxy: 449 passed in 7.06s
  - Governance: 88 passed, 8 warnings in 18.74s
  - MCP proxy: 55 passed in 1.14s
  - Root tests: 24 passed in 0.63s
- `make lint`: passed

## Fixes

- None required after implementation.

## Notes

- Governance warnings are the existing Starlette/httpx test client and torch JIT deprecation warnings.
