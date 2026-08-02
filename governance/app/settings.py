from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Task 6: use Pydantic v2 model_config instead of inner class Config
    model_config = SettingsConfigDict(env_file=".env", populate_by_name=True)

    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    redis_url: str = "redis://localhost:6379"
    opa_url: str = "http://localhost:8181"
    spacy_model: str = "en_core_web_lg"
    pii_backend: Literal["google", "presidio"] = "google"
    google_cloud_project: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT_ID"),
    )
    google_dlp_location: str = "global"
    google_dlp_api_endpoint: str | None = None
    google_dlp_min_likelihood: Literal[
        "VERY_UNLIKELY", "UNLIKELY", "POSSIBLE", "LIKELY", "VERY_LIKELY"
    ] = "POSSIBLE"
    google_dlp_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    google_dlp_info_types: str = (
        "EMAIL_ADDRESS,PHONE_NUMBER,US_SOCIAL_SECURITY_NUMBER,CREDIT_CARD_NUMBER,"
        "IP_ADDRESS,STREET_ADDRESS,DATE_OF_BIRTH"
    )
    # Task 5: cors_origins configurable via env instead of hardcoded in main.py
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:8000"]
    internal_token: str = Field(
        ...,
        validation_alias=AliasChoices("GOVERNANCE_INTERNAL_TOKEN", "INTERNAL_TOKEN"),
    )
    pseudonym_hmac_key: str = Field(...)
    entitlements_rego_path: str = "/policies/mcp/authz.rego"

    @property
    def google_dlp_info_type_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            item.strip() for item in self.google_dlp_info_types.split(",") if item.strip()
        ))

    @model_validator(mode="after")
    def validate_pii_backend(self) -> "Settings":
        if self.pii_backend == "google":
            if not self.google_cloud_project:
                raise ValueError("PII_BACKEND=google requires GOOGLE_CLOUD_PROJECT")
            if not self.google_dlp_location or "/" in self.google_dlp_location:
                raise ValueError("GOOGLE_DLP_LOCATION must be a location id such as global or us")
            if self.google_dlp_api_endpoint and "://" in self.google_dlp_api_endpoint:
                raise ValueError("GOOGLE_DLP_API_ENDPOINT must be a hostname, not a URL")
            if not self.google_dlp_info_type_names:
                raise ValueError("GOOGLE_DLP_INFO_TYPES must contain at least one info type")
        return self


settings = Settings()  # pyright: ignore[reportCallIssue]

__all__ = ["Settings", "settings"]
