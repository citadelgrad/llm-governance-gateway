from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379"
    governance_url: str = "http://governance:8000"
    governance_internal_token: str = Field(...)
    mcpproxy_url: str = "http://mcpproxy:8000"
    agent_runtime_client_ids: list[str] = []
    database_url: str = Field(..., description="PostgreSQL connection string (required)")
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    # New: Vertex AI (SA-authenticated) path. Prefixed distinctly from
    # governance's GOOGLE_DLP_* settings even though the underlying GCP
    # project id may coincide (both services configure Google Cloud
    # access independently).
    gemini_vertex_project_id: str = ""
    gemini_vertex_location: str = ""  # e.g. "us-central1", or "global"
    gemini_vertex_credentials_path: str = ""  # optional; falls back to
    # standard ADC discovery (GOOGLE_APPLICATION_CREDENTIALS or
    # gcloud ADC file) if unset
    gemini_vertex_expected_service_account: str = ""  # for startup
    # validation, mirroring scripts/google_adc_keychain.py's
    # --expected-service-account pattern
    gemini_vertex_timeout_seconds: float = 60.0
    ollama_base_url: str = "http://localhost:11434/v1"
    mock_providers: bool = False
    mock_mode: bool = False
    docs_enabled: bool = False
    mock_stream_delay_ms: int = 0
    models_yaml: str = "config/models.yaml"
    jwt_secret: str = Field(...)
    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60
    cors_origins: list[str] = ["http://localhost:3000"]

    model_config = {"env_file": ".env"}

    @model_validator(mode="after")
    def resolve_mock_mode(self) -> "Settings":
        self.mock_mode = self.mock_providers or self.openai_api_key == "mock"
        return self


settings = Settings()  # pyright: ignore[reportCallIssue]
