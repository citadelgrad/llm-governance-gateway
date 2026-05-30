from __future__ import annotations

import asyncio
import json

from proxy.app.config import settings
from proxy.app.headers import error_envelope
from starlette.responses import Response, StreamingResponse
from tests.fixtures.mock_scenarios import ALL_SCENARIOS, MockScenario


def _match_scenario(messages: list[dict]) -> MockScenario:
    for scenario in ALL_SCENARIOS:
        if scenario.name != "clean_request" and scenario.trigger(messages):
            return scenario
    return next((s for s in ALL_SCENARIOS if s.name == "clean_request"), ALL_SCENARIOS[0])


async def _stream_sse(text: str, model: str, delay_ms: int):
    chunk_size = 5
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    for chunk in chunks:
        payload = json.dumps(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"content": chunk}, "finish_reason": None}
                ],
            }
        )
        yield f"data: {payload}\n\n"
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)

    final = json.dumps(
        {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
        }
    )
    yield f"data: {final}\n\n"
    yield "data: [DONE]\n\n"


async def chat_completions(
    body: dict,
    extra_headers: dict[str, str],
) -> Response | StreamingResponse:
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    model = body.get("model", "mock")
    scenario = _match_scenario(messages)
    delay_ms = settings.mock_stream_delay_ms

    if scenario.decision == "block":
        return Response(
            content=json.dumps(
                error_envelope(
                    error_type="policy_violation",
                    message="Request blocked by policy",
                    violations=scenario.violations,
                )
            ),
            status_code=scenario.expected_status,
            media_type="application/json",
            headers=extra_headers,
        )

    if stream:
        return StreamingResponse(
            _stream_sse(scenario.response_text, model, delay_ms),
            media_type="text/event-stream",
            headers=extra_headers,
        )

    return Response(
        content=json.dumps(
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": scenario.response_text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        ),
        status_code=200,
        media_type="application/json",
        headers=extra_headers,
    )
