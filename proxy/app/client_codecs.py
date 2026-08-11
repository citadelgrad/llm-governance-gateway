from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException
from proxy.app.anthropic_compat import (
    AnthropicCompatError,
    AnthropicGatewayPayload,
    AnthropicMessagesRequest,
    CountTokensRequest,
    chat_response_to_anthropic,
    count_tokens_approximate,
    openai_sse_to_anthropic_sse,
)
from proxy.app.headers import error_envelope
from proxy.app.protocol_types import (
    GatewayPayload,
    JsonObject,
    OpenAIChatPayload,
    OpenAIChatRequest,
    WireProtocol,
    format_validation_location,
)
from proxy.app.responses_compat import (
    ResponsesCompatError,
    ResponsesCreateRequest,
    ResponsesGatewayPayload,
    openai_sse_to_responses_sse,
    translate_chat_response,
)
from pydantic import ValidationError
from starlette.responses import Response, StreamingResponse


@dataclass(frozen=True)
class PipelineResult:
    response: Response | StreamingResponse
    extra_headers: dict[str, str]
    response_protocol: WireProtocol
    client_model: str


class ClientWireCodec(Protocol):
    def decode_payload(self, body: JsonObject) -> GatewayPayload: ...

    def encode_response(
        self, result: PipelineResult
    ) -> Response | StreamingResponse: ...


def _validation_violations(exc: ValidationError, *, format_location: bool) -> list[dict[str, str]]:
    def field(error: Mapping[str, Any]) -> str:
        loc = error["loc"]
        if not isinstance(loc, tuple):
            return str(loc)
        if format_location:
            return format_validation_location(loc)
        return ".".join(str(part) for part in loc)

    return [
        {
            "field": field(error),
            "type": error["type"],
            "message": error["msg"],
        }
        for error in exc.errors(include_url=False, include_input=False)
    ]


class OpenAIChatCodec:
    def decode_payload(self, body: JsonObject) -> GatewayPayload:
        try:
            request = OpenAIChatRequest.model_validate(body)
        except ValidationError as exc:
            violations = sorted(
                _validation_violations(exc, format_location=True),
                key=lambda violation: str(violation["field"]).count("."),
                reverse=True,
            )
            raise HTTPException(
                status_code=400,
                detail=error_envelope(
                    "invalid_request",
                    "Request body is not a valid OpenAI Chat Completions request",
                    details={"violations": violations},
                ),
            ) from exc
        return OpenAIChatPayload(request)

    def encode_response(
        self, result: PipelineResult
    ) -> Response | StreamingResponse:
        return result.response


class ResponsesCodec:
    def decode_payload(self, body: JsonObject) -> GatewayPayload:
        try:
            request = ResponsesCreateRequest.model_validate(body)
        except ValidationError as exc:
            violations = _validation_violations(exc, format_location=True)
            raise HTTPException(
                status_code=400,
                detail={
                    **error_envelope(
                        "invalid_request",
                        "Request body is not a valid OpenAI Responses request",
                    ),
                    "violations": violations,
                },
            ) from exc
        return ResponsesGatewayPayload(request)

    def encode_response(
        self, result: PipelineResult
    ) -> Response | StreamingResponse:
        response = result.response
        if result.response_protocol == "openai_responses":
            return response
        if isinstance(response, StreamingResponse):
            translated = openai_sse_to_responses_sse(response.body_iterator, result.client_model)
            return StreamingResponse(
                translated,
                media_type="text/event-stream",
                headers=result.extra_headers,
            )
        if response.status_code >= 400:
            return response

        try:
            chat_body = json.loads(bytes(response.body))
            translated_body = translate_chat_response(chat_body)
        except (json.JSONDecodeError, ValueError, ResponsesCompatError) as exc:
            raise HTTPException(
                status_code=502,
                detail=error_envelope(
                    "invalid_upstream_response",
                    "Provider returned an unexpected response shape",
                ),
            ) from exc

        response_headers = dict(response.headers)
        response_headers.pop("content-length", None)
        return Response(
            content=json.dumps(translated_body),
            status_code=response.status_code,
            media_type="application/json",
            headers=response_headers,
        )

class AnthropicMessagesCodec:
    def decode_payload(self, body: JsonObject) -> GatewayPayload:
        try:
            request = AnthropicMessagesRequest.model_validate(body)
        except ValidationError as exc:
            violations = _validation_violations(exc, format_location=False)
            raise HTTPException(
                status_code=400,
                detail={
                    **error_envelope(
                        "invalid_request",
                        "Request body is not a valid Anthropic Messages request",
                    ),
                    "violations": violations,
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=error_envelope(
                    "invalid_request",
                    "Request body is not a valid Anthropic Messages request",
                ),
            ) from exc
        return AnthropicGatewayPayload(request)

    def encode_response(
        self, result: PipelineResult
    ) -> Response | StreamingResponse:
        response = result.response
        if result.response_protocol == "anthropic_messages":
            return response

        model = result.client_model
        if isinstance(response, StreamingResponse):
            translated = openai_sse_to_anthropic_sse(response.body_iterator, model)
            return StreamingResponse(
                translated,
                media_type="text/event-stream",
                headers=result.extra_headers,
            )

        if response.status_code != 200:
            return response

        try:
            chat_json = json.loads(bytes(response.body))
            translated_body = chat_response_to_anthropic(chat_json, model)
        except (json.JSONDecodeError, ValueError, AnthropicCompatError):
            return response

        response_headers = dict(response.headers)
        response_headers.update(result.extra_headers)
        response_headers.pop("content-length", None)
        return Response(
            content=json.dumps(translated_body),
            status_code=200,
            media_type="application/json",
            headers=response_headers,
        )

class CountTokensCodec:
    def decode_request(self, body: JsonObject) -> CountTokensRequest:
        try:
            return CountTokensRequest.model_validate(body)
        except ValidationError as exc:
            violations = _validation_violations(exc, format_location=False)
            raise HTTPException(
                status_code=400,
                detail={
                    **error_envelope("invalid_request", "Request body is not valid"),
                    "violations": violations,
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=error_envelope("invalid_request", "Request body is not valid"),
            ) from exc

    def encode_response(self, request: CountTokensRequest) -> JsonObject:
        try:
            input_tokens = count_tokens_approximate(request.messages, request.system)
        except AnthropicCompatError as exc:
            raise HTTPException(
                status_code=422,
                detail=error_envelope("unsupported_message_shape", str(exc)),
            ) from exc
        return {"input_tokens": input_tokens}
