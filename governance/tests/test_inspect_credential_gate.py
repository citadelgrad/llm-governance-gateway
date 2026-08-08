"""ai-gateway-mbk8: /inspect (and /v1/dlp/pii-scan) must only enforce the
Google ADC freshness gate when settings.pii_backend == "google". Presidio
deployments never touch Google DLP, so a stale/missing
google-credential-sentinel status must not make every request 503.

These tests hit the FastAPI app directly via httpx.ASGITransport (no
lifespan, no TestClient) - the same pattern used in
test_google_credential_sentinel.py - so they run fast and do not require a
live OPA/Postgres/Docker stack. Scenario 3 (Docker healthcheck target) is
verified separately via `docker compose config` (see final report); it is
not exercised here since it is a Docker Compose concern, not app behavior.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app import google_credential_sentinel as sentinel
from app import main as governance_main

VALID_INSPECT_BODY = {
    "text": "hello world",
    "tenant_id": "tenant",
    "user_id": "user",
    "model_id": "model",
    "routing_method": "direct",
}


async def _post_inspect(headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=governance_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://governance") as client:
        return await client.post("/inspect", headers=headers, json=VALID_INSPECT_BODY)


async def _post_pii_scan(headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=governance_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://governance") as client:
        return await client.post("/v1/dlp/pii-scan", headers=headers, json={"text": "hello world"})


@pytest.mark.asyncio
async def test_presidio_backend_ignores_missing_google_status(tmp_path, monkeypatch):
    """AC Scenario 1: PII_BACKEND=presidio + stale/missing sentinel status
    -> /inspect returns 200 with a valid decision, no PiiBackendError/503."""
    monkeypatch.setattr(governance_main.settings, "pii_backend", "presidio")
    monkeypatch.setattr(
        governance_main.settings,
        "google_adc_status_path",
        str(tmp_path / "missing-status.json"),
    )

    response = await _post_inspect(
        headers={"X-Internal-Token": governance_main.settings.internal_token}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] in {"allow", "block"}
    assert "redacted_text" in body
    assert "pii_findings" in body


@pytest.mark.asyncio
async def test_presidio_backend_ignores_stale_google_status(tmp_path, monkeypatch):
    """Same as above but the status file exists and is simply stale, rather
    than missing outright - both must be treated identically when the
    backend is presidio."""
    monkeypatch.setattr(governance_main.settings, "pii_backend", "presidio")
    now = datetime(2026, 8, 5, 11, 0, tzinfo=UTC)
    monkeypatch.setattr(sentinel, "_now", lambda: now)
    path = tmp_path / "status.json"
    sentinel.write_status(
        path,
        {
            "status": "ready",
            "checked_at": "2026-08-05T09:00:00Z",  # >90s stale
        },
    )
    monkeypatch.setattr(governance_main.settings, "google_adc_status_path", str(path))

    response = await _post_inspect(
        headers={"X-Internal-Token": governance_main.settings.internal_token}
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_google_backend_rejects_missing_status(tmp_path, monkeypatch):
    """AC Scenario 2: PII_BACKEND=google + stale/missing sentinel status ->
    /inspect returns 503 with reason 'Google credentials unavailable'."""
    monkeypatch.setattr(governance_main.settings, "pii_backend", "google")
    monkeypatch.setattr(
        governance_main.settings,
        "google_adc_status_path",
        str(tmp_path / "missing-status.json"),
    )

    response = await _post_inspect(
        headers={"X-Internal-Token": governance_main.settings.internal_token}
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "PII inspection temporarily unavailable"}
    assert response.headers["retry-after"] == str(
        governance_main.settings.google_adc_retry_after_seconds
    )


@pytest.mark.asyncio
async def test_google_backend_healthy_status_returns_200(tmp_path, monkeypatch):
    """AC Scenario 4 (regression): PII_BACKEND=google + healthy/fresh status
    -> /inspect returns 200 with a valid decision, unchanged behavior."""
    monkeypatch.setattr(governance_main.settings, "pii_backend", "google")
    now = datetime(2026, 8, 5, 11, 0, tzinfo=UTC)
    monkeypatch.setattr(sentinel, "_now", lambda: now)
    path = tmp_path / "status.json"
    sentinel.write_status(
        path,
        {
            "status": "ready",
            "checked_at": "2026-08-05T11:00:00Z",  # fresh
        },
    )
    monkeypatch.setattr(governance_main.settings, "google_adc_status_path", str(path))

    response = await _post_inspect(
        headers={"X-Internal-Token": governance_main.settings.internal_token}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] in {"allow", "block"}


@pytest.mark.asyncio
async def test_pii_scan_presidio_backend_ignores_missing_google_status(tmp_path, monkeypatch):
    """Same gating fix applied to /v1/dlp/pii-scan (identical duplicated
    logic, reachable via the MCP proxy path) - presidio backend must not be
    blocked by a missing Google ADC status."""
    monkeypatch.setattr(governance_main.settings, "pii_backend", "presidio")
    monkeypatch.setattr(
        governance_main.settings,
        "google_adc_status_path",
        str(tmp_path / "missing-status.json"),
    )

    response = await _post_pii_scan(
        headers={"X-Internal-Token": governance_main.settings.internal_token}
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_pii_scan_google_backend_rejects_missing_status(tmp_path, monkeypatch):
    """Regression: /v1/dlp/pii-scan still enforces the gate for the google
    backend."""
    monkeypatch.setattr(governance_main.settings, "pii_backend", "google")
    monkeypatch.setattr(
        governance_main.settings,
        "google_adc_status_path",
        str(tmp_path / "missing-status.json"),
    )

    response = await _post_pii_scan(
        headers={"X-Internal-Token": governance_main.settings.internal_token}
    )

    assert response.status_code == 503
