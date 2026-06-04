from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import tenacity
from proxy.app.config import settings


class GovernanceError(Exception):
    pass


@dataclass
class InspectRequest:
    text: str
    tenant_id: str
    user_id: str
    model_id: str
    routing_method: str
    phase: str = "pre_call"
    roles: list[str] = field(default_factory=list)


@dataclass
class InspectResponse:
    decision: str
    redacted_text: str
    pii_findings: list[dict]
    harm_score: float
    violations: list[str]
    audit_id: str | None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


class GovernanceClient:
    def __init__(self, client: httpx.AsyncClient, internal_token: str) -> None:
        self._client = client
        self._token = internal_token

    async def inspect(self, req: InspectRequest) -> InspectResponse:
        """Fail-closed: raises GovernanceError on any failure after retries."""

        @tenacity.retry(
            stop=tenacity.stop_after_attempt(3),
            wait=tenacity.wait_exponential(multiplier=0.5, max=4),
            retry=tenacity.retry_if_exception(_is_retryable),
            reraise=True,
        )
        async def _call() -> InspectResponse:
            try:
                resp = await self._client.post(
                    f"{settings.governance_url}/inspect",
                    headers={"X-Internal-Token": self._token},
                    json={
                        "text": req.text,
                        "tenant_id": req.tenant_id,
                        "user_id": req.user_id,
                        "model_id": req.model_id,
                        "routing_method": req.routing_method,
                        "phase": req.phase,
                        "roles": req.roles,
                    },
                )
                resp.raise_for_status()
            except (httpx.RequestError, httpx.HTTPStatusError):
                raise
            except Exception as exc:
                raise GovernanceError(f"Unexpected governance error: {exc}") from exc

            data = resp.json()
            return InspectResponse(
                decision=data["decision"],
                redacted_text=data["redacted_text"],
                pii_findings=data.get("pii_findings", []),
                harm_score=data.get("harm_score", 0.0),
                violations=data.get("violations", []),
                audit_id=data.get("audit_id"),
            )

        try:
            return await _call()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise GovernanceError(f"Governance unreachable after retries: {exc}") from exc


def make_governance_client(client: httpx.AsyncClient) -> GovernanceClient:
    return GovernanceClient(client, settings.governance_internal_token)


def extract_user_message(body: dict) -> str:
    """Extract the last user message text from a chat completion request body."""
    messages = body.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
    return ""
