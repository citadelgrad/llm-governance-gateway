from __future__ import annotations

import json
import secrets

import httpx
from starlette.responses import Response, StreamingResponse

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

_FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
}


def make_client(api_key: str) -> httpx.AsyncClient:
    """Create the shared Gemini client. Auth uses ?key= query param."""
    client = httpx.AsyncClient(
        base_url=GEMINI_BASE,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
    )
    client.params = httpx.QueryParams({"key": api_key})
    return client


def _translate_request(body: dict) -> tuple[str, dict]:
    """Translate an OpenAI chat completions body to a Gemini generateContent body.

    Returns (model_id, gemini_body).
    """
    model: str = body.get("model", "gemini-1.5-flash")
    messages: list[dict] = body.get("messages", [])

    system_parts: list[str] = []
    contents: list[dict] = []

    for msg in messages:
        role: str = msg.get("role", "user")
        content = msg.get("content", "")

        # Normalise content to a plain string
        if isinstance(content, list):
            text = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        else:
            text = str(content)

        if role == "system":
            system_parts.append(text)
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text}]})
        else:
            # user / tool / function — map to "user" and skip non-text roles
            if role in ("tool", "function"):
                continue
            contents.append({"role": "user", "parts": [{"text": text}]})

    gemini_body: dict = {"contents": contents}

    if system_parts:
        gemini_body["systemInstruction"] = {
            "parts": [{"text": "\n".join(system_parts)}]
        }

    generation_config: dict = {}
    if "temperature" in body:
        generation_config["temperature"] = body["temperature"]
    if "top_p" in body:
        generation_config["topP"] = body["top_p"]
    if "max_tokens" in body:
        generation_config["maxOutputTokens"] = body["max_tokens"]
    if "stop" in body:
        stop = body["stop"]
        generation_config["stopSequences"] = [stop] if isinstance(stop, str) else stop

    if generation_config:
        gemini_body["generationConfig"] = generation_config

    return model, gemini_body


def _to_openai_envelope(gemini_json: dict, model: str) -> dict:
    """Convert a Gemini generateContent response to an OpenAI chat.completion envelope."""
    candidates = gemini_json.get("candidates", [])
    usage_meta = gemini_json.get("usageMetadata", {})

    text_parts: list[str] = []
    finish_reason = "stop"

    if candidates:
        candidate = candidates[0]
        content_obj = candidate.get("content", {})
        for part in content_obj.get("parts", []):
            if "text" in part:
                text_parts.append(part["text"])
        gemini_finish = candidate.get("finishReason", "STOP")
        finish_reason = _FINISH_REASON_MAP.get(gemini_finish, "stop")

    return {
        "id": f"chatcmpl-gemini-{secrets.token_hex(8)}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "".join(text_parts),
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage_meta.get("promptTokenCount", 0),
            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
            "total_tokens": usage_meta.get("totalTokenCount", 0),
        },
    }


async def chat_completions(
    client: httpx.AsyncClient,
    body: dict,
    stream: bool,
    extra_headers: dict[str, str],
) -> Response | StreamingResponse:
    """Forward to Gemini and return a Starlette Response in OpenAI shape."""
    model, gemini_body = _translate_request(body)

    if stream:
        url = f"/models/{model}:streamGenerateContent"

        try:

            async def _stream_body(upstream_ctx):  # type: ignore[no-untyped-def]
                completion_id = f"chatcmpl-gemini-{secrets.token_hex(8)}"
                async with upstream_ctx as upstream:
                    async for line in upstream.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        raw = line[len("data:") :].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk_json = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        candidates = chunk_json.get("candidates", [])
                        if not candidates:
                            continue
                        candidate = candidates[0]
                        parts = candidate.get("content", {}).get("parts", [])
                        text = "".join(p.get("text", "") for p in parts)

                        oai_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": text},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(oai_chunk)}\n\n"

                # Final chunk
                final_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            req = client.stream("POST", url, json=gemini_body, params={"alt": "sse"})
            return StreamingResponse(
                _stream_body(req),
                status_code=200,
                media_type="text/event-stream",
                headers=extra_headers,
            )
        except httpx.TimeoutException:
            return Response(content=b"upstream timeout", status_code=504)
        except httpx.RequestError:
            return Response(content=b"upstream connection error", status_code=502)

    else:
        url = f"/models/{model}:generateContent"
        try:
            upstream = await client.post(url, json=gemini_body)
        except httpx.TimeoutException:
            return Response(content=b"upstream timeout", status_code=504)
        except httpx.RequestError:
            return Response(content=b"upstream connection error", status_code=502)

        if upstream.status_code != 200:
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type"),
                headers=extra_headers,
            )

        try:
            gemini_json = upstream.json()
            envelope = _to_openai_envelope(gemini_json, model)
        except Exception:
            # Fallback: pass through raw response if parsing fails
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type"),
                headers=extra_headers,
            )

        return Response(
            content=json.dumps(envelope).encode(),
            status_code=200,
            media_type="application/json",
            headers=extra_headers,
        )
