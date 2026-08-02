import httpx
from proxy.app.providers.errors import sanitize_upstream_error
from starlette.responses import Response, StreamingResponse

OPENAI_BASE = "https://api.openai.com/v1"
_MAX_COMPLETION_TOKEN_MODELS = ("gpt-5", "o1", "o3", "o4")
_TOOLS_REQUIRE_NO_REASONING_MODELS = ("gpt-5.6-luna",)


def make_client(api_key: str) -> httpx.AsyncClient:
    """Create the shared client. Call once at lifespan startup."""
    return httpx.AsyncClient(
        base_url=OPENAI_BASE,
        headers={"Authorization": f"Bearer {api_key}"},
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
    )


def _translate_request(body: dict) -> dict:
    """Translate legacy Chat Completions controls for newer OpenAI models."""
    translated = dict(body)
    model = str(translated.get("model", "")).lower()
    if (
        model.startswith(_MAX_COMPLETION_TOKEN_MODELS)
        and "max_tokens" in translated
        and "max_completion_tokens" not in translated
    ):
        translated["max_completion_tokens"] = translated.pop("max_tokens")
    if (
        model.startswith(_TOOLS_REQUIRE_NO_REASONING_MODELS)
        and translated.get("tools")
        and "reasoning_effort" not in translated
    ):
        translated["reasoning_effort"] = "none"
    return translated


async def chat_completions(
    client: httpx.AsyncClient,
    body: dict,
    stream: bool,
    extra_headers: dict[str, str],
) -> Response | StreamingResponse:
    """Forward to OpenAI. Returns a Starlette Response."""
    upstream_body = _translate_request(body)
    if stream:
        request = client.build_request("POST", "/chat/completions", json=upstream_body)
        try:
            upstream = await client.send(request, stream=True)
        except httpx.TimeoutException:
            return Response(content=b"upstream timeout", status_code=504)
        except httpx.RequestError:
            return Response(content=b"upstream connection error", status_code=502)

        if upstream.status_code != 200:
            await upstream.aread()
            response = sanitize_upstream_error(upstream, extra_headers, provider="openai")
            await upstream.aclose()
            return response

        async def _stream_body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            except httpx.TimeoutException:
                yield b'data: {"error": {"type": "upstream_timeout"}}\n\n'
            except httpx.RequestError:
                yield b'data: {"error": {"type": "upstream_connection_error"}}\n\n'
            finally:
                await upstream.aclose()

        return StreamingResponse(
            _stream_body(),
            status_code=200,
            media_type="text/event-stream",
            headers=extra_headers,
        )

    try:
        upstream = await client.post("/chat/completions", json=upstream_body)
    except httpx.TimeoutException:
        return Response(content=b"upstream timeout", status_code=504)
    except httpx.RequestError:
        return Response(content=b"upstream connection error", status_code=502)

    if upstream.status_code != 200:
        return sanitize_upstream_error(upstream, extra_headers, provider="openai")

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=extra_headers,
    )
