from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

MAX_BODY_SIZE = 1 * 1024 * 1024


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_BODY_SIZE:
            return Response(content="Request body too large (max 1MB)", status_code=413)
        # Also cap streaming bodies that omit Content-Length
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY_SIZE:
                return Response(content="Request body too large (max 1MB)", status_code=413)
        # Store so downstream handlers can read it
        request._body = body
        return await call_next(request)
