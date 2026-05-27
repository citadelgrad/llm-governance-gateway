from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic_settings import BaseSettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response as StarletteResponse


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    redis_url: str = "redis://localhost:6379"
    opa_url: str = "http://localhost:8181"
    spacy_model: str = "en_core_web_lg"

    class Config:
        env_file = ".env"


settings = Settings()
_ready = False
_nlp_engine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ready, _nlp_engine
    # Initialize Presidio NLP engine (loads spaCy model — slow on first start)
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    provider = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": settings.spacy_model}],
    })
    _nlp_engine = provider.create_engine()
    _ready = True
    yield
    _ready = False


app = FastAPI(title="AI Gateway Governance", lifespan=lifespan)

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
            return StarletteResponse(
                content="Request body too large (max 1MB)",
                status_code=413,
            )
        return await call_next(request)


app.add_middleware(BodySizeLimitMiddleware)


@app.get("/health")
async def health():
    if not _ready:
        return Response(status_code=503, content="starting")

    # Check for stuck partitions: detached_at > 48h AND dumped_at IS NULL
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text
        from datetime import datetime, timedelta, timezone

        engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0)
        async with engine.connect() as conn:
            result = await conn.execute(text(
                "SELECT COUNT(*) FROM partition_archive_state "
                "WHERE detached_at < NOW() - INTERVAL '48 hours' "
                "AND dumped_at IS NULL"
            ))
            stuck = result.scalar()
        await engine.dispose()

        if stuck and stuck > 0:
            return {"status": "degraded", "reason": f"{stuck} stuck partition(s)"}
    except Exception:
        # DB not yet available — don't fail health check for this
        pass

    return {"status": "ok"}
