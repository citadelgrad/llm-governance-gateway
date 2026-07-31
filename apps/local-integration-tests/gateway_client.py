from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


def _load_root_env() -> None:
    """Load the gateway checkout's local .env without printing secrets.

    This keeps the live-test app aligned with the JWT secret and local ports used
    by Docker Compose, while still letting explicit shell env vars override it.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


DEFAULT_BASE_URL = "http://localhost:18765"
DEFAULT_JWT_SECRET = "local-dev-jwt-secret-for-compose-tests-only"
DEFAULT_TENANT_ID = "local-integration"
DEFAULT_USER_ID = f"local-integration-user-{int(time.time())}"
DEFAULT_ROLES = ["tier1"]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(
    *,
    secret: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    roles: list[str] | None = None,
    ttl_seconds: int = 900,
) -> str:
    secret = secret or os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "user_id": user_id or os.environ.get("GATEWAY_USER_ID", DEFAULT_USER_ID),
        "tenant_id": tenant_id or os.environ.get("GATEWAY_TENANT_ID", DEFAULT_TENANT_ID),
        "roles": roles or DEFAULT_ROLES,
        "iat": int(time.time()),
        "nbf": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
    }
    signing_input = ".".join(
        [
            _b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(payload, separators=(",", ":")).encode()),
        ]
    )
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


@dataclass
class GatewayClient:
    base_url: str | None = None
    token: str | None = None

    def __post_init__(self) -> None:
        _load_root_env()
        if self.base_url is None:
            self.base_url = os.environ.get("GATEWAY_BASE_URL", DEFAULT_BASE_URL)
        if self.token is None:
            self.token = make_jwt()

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            response = await client.get("/health")
            response.raise_for_status()
            return response.json()

    async def me(self) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            response = await client.get("/v1/me", headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def models(self) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            response = await client.get("/v1/models", headers=self.headers)
            response.raise_for_status()
            return response.json()

    async def chat(self, prompt: str, *, model: str = "gpt-5.6-luna") -> httpx.Response:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            return await client.post(
                "/v1/chat/completions",
                headers=self.headers,
                json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            )

    async def responses(self, prompt: str, *, model: str = "gpt-5.6-luna") -> httpx.Response:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            return await client.post(
                "/v1/responses",
                headers=self.headers,
                json={"model": model, "input": prompt},
            )
