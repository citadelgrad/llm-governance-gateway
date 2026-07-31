from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    governance_url: str = "http://governance:8000"
    governance_internal_token: str = Field(...)
    # Fixed by the shared network namespace (network_mode: "service:mcpproxy"
    # in docker-compose.yml), not environment-specific - no env var required.
    opa_sidecar_url: str = "http://127.0.0.1:8181"
    # Break-glass fallback while the OPA Sidecar circuit breaker is open:
    # "{server}:{tool}" entries, matching the entitlement matrix's (server,
    # tool) keying (policies/mcp/authz.rego) - never sourced from that
    # document itself, since it's unreachable through the down sidecar.
    breakglass_tool_allowlist: list[str] = ["github-mcp:list_prs"]

    model_config = {"env_file": ".env"}


settings = Settings()  # pyright: ignore[reportCallIssue]
