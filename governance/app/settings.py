from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    redis_url: str = "redis://localhost:6379"
    opa_url: str = "http://localhost:8181"
    spacy_model: str = "en_core_web_lg"
    internal_token: str = Field(
        ...,
        validation_alias=AliasChoices("GOVERNANCE_INTERNAL_TOKEN", "INTERNAL_TOKEN"),
    )
    pseudonym_hmac_key: str = Field(...)

    class Config:
        env_file = ".env"


settings = Settings()

__all__ = ["Settings", "settings"]
