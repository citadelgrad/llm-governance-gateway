from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Task 6: use Pydantic v2 model_config instead of inner class Config
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    redis_url: str = "redis://localhost:6379"
    opa_url: str = "http://localhost:8181"
    spacy_model: str = "en_core_web_lg"
    # Task 5: cors_origins configurable via env instead of hardcoded in main.py
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:8000"]
    internal_token: str = Field(
        ...,
        validation_alias=AliasChoices("GOVERNANCE_INTERNAL_TOKEN", "INTERNAL_TOKEN"),
    )
    pseudonym_hmac_key: str = Field(...)
    entitlements_rego_path: str = "/policies/mcp/authz.rego"


settings = Settings()  # pyright: ignore[reportCallIssue]

__all__ = ["Settings", "settings"]
