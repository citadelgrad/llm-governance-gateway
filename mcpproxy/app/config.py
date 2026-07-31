from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    governance_url: str = "http://governance:8000"
    governance_internal_token: str = Field(...)

    model_config = {"env_file": ".env"}


settings = Settings()  # pyright: ignore[reportCallIssue]
