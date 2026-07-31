from __future__ import annotations

from dataclasses import dataclass

import httpx
from mcpproxy.app.config import settings

DLP_SCAN_TIMEOUT_SECONDS = 5.0


class DlpScanError(Exception):
    """Raised on any DLP scan failure. Callers must treat this as fail-closed (block)."""


class AuditEventError(Exception):
    """Raised on any audit-event write failure."""


@dataclass
class DlpScanResult:
    pii_findings: list[dict]
    data_classification: str
    redacted_text: str


class GovernanceClient:
    def __init__(self, client: httpx.AsyncClient, internal_token: str) -> None:
        self._client = client
        self._token = internal_token

    async def scan_for_pii(self, text: str) -> DlpScanResult:
        """No retries: a single failed attempt is enough to fail closed."""
        try:
            resp = await self._client.post(
                f"{settings.governance_url}/v1/dlp/pii-scan",
                headers={"X-Internal-Token": self._token},
                json={"text": text},
                timeout=DLP_SCAN_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
        except Exception as exc:
            raise DlpScanError(f"DLP scan failed: {exc}") from exc

        data = resp.json()
        return DlpScanResult(
            pii_findings=data.get("pii_findings", []),
            data_classification=data.get("data_classification", ""),
            redacted_text=data.get("redacted_text", ""),
        )

    async def send_audit_event(
        self, *, tenant_id: str, user_id: str, event_type: str, decision: str
    ) -> None:
        try:
            resp = await self._client.post(
                f"{settings.governance_url}/v1/mcp/audit-event",
                headers={"X-Internal-Token": self._token},
                json={
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "event_type": event_type,
                    "decision": decision,
                },
                timeout=DLP_SCAN_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
        except Exception as exc:
            raise AuditEventError(f"Audit event write failed: {exc}") from exc


def make_governance_client(client: httpx.AsyncClient) -> GovernanceClient:
    return GovernanceClient(client, settings.governance_internal_token)
