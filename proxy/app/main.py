from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic_settings import BaseSettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379"
    governance_url: str = "http://localhost:8000"
    governance_internal_token: str = "dev-internal-token"

    class Config:
        env_file = ".env"


settings = Settings()
_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ready
    # Verify governance /health is reachable
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{settings.governance_url}/health")
        resp.raise_for_status()
    _ready = True
    yield
    _ready = False


app = FastAPI(title="AI Gateway Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

MAX_BODY_SIZE = 1 * 1024 * 1024  # 1MB


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_BODY_SIZE:
            return Response(
                content="Request body too large (max 1MB)",
                status_code=413,
            )
        return await call_next(request)


app.add_middleware(BodySizeLimitMiddleware)


@app.get("/health")
async def health():
    if not _ready:
        return Response(status_code=503, content="starting")
    return {"status": "ok"}
