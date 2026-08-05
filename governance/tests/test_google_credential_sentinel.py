from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app import google_credential_sentinel as sentinel
from app import main as governance_main
from app.google_credentials import GoogleCredentialPreflightError


def test_probe_publishes_only_metadata(monkeypatch):
    expiry = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sentinel, "_now", lambda: datetime(2026, 8, 5, 11, 0, tzinfo=UTC))
    monkeypatch.setattr(
        sentinel,
        "preflight",
        lambda **_kwargs: SimpleNamespace(
            credentials=SimpleNamespace(token="must-not-leak"),
            service_account_email="dlp@example.iam.gserviceaccount.com",
            expiry=expiry,
        ),
    )

    status = sentinel.probe("dlp@example.iam.gserviceaccount.com")

    assert status == {
        "status": "ready",
        "checked_at": "2026-08-05T11:00:00Z",
        "service_account": "dlp@example.iam.gserviceaccount.com",
        "token_expiry": "2026-08-05T12:00:00Z",
    }
    assert "must-not-leak" not in json.dumps(status)


def test_probe_sanitizes_refresh_failure(monkeypatch):
    def fail(**_kwargs):
        raise GoogleCredentialPreflightError("provider detail must not leak")

    monkeypatch.setattr(sentinel, "preflight", fail)
    monkeypatch.setattr(sentinel, "_now", lambda: datetime(2026, 8, 5, 11, 0, tzinfo=UTC))

    assert sentinel.probe("dlp@example.iam.gserviceaccount.com") == {
        "status": "unavailable",
        "checked_at": "2026-08-05T11:00:00Z",
        "reason": "credential_refresh_failed",
    }


def test_status_file_is_private_and_fresh(tmp_path, monkeypatch):
    now = datetime(2026, 8, 5, 11, 0, tzinfo=UTC)
    monkeypatch.setattr(sentinel, "_now", lambda: now)
    path = tmp_path / "private" / "status.json"
    payload = {"status": "ready", "checked_at": "2026-08-05T11:00:00Z"}

    sentinel.write_status(path, payload)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sentinel.read_status(path, max_age_seconds=90) == payload
    assert sentinel.credentials_ready(str(path), max_age_seconds=90)


def test_stale_or_unavailable_status_is_not_ready(tmp_path, monkeypatch):
    now = datetime(2026, 8, 5, 11, 0, tzinfo=UTC)
    monkeypatch.setattr(sentinel, "_now", lambda: now)
    path = tmp_path / "status.json"
    sentinel.write_status(
        path,
        {
            "status": "ready",
            "checked_at": (now - timedelta(seconds=91)).isoformat().replace("+00:00", "Z"),
        },
    )
    assert not sentinel.credentials_ready(str(path), max_age_seconds=90)

    sentinel.write_status(
        path,
        {"status": "unavailable", "checked_at": now.isoformat().replace("+00:00", "Z")},
    )
    assert not sentinel.credentials_ready(str(path), max_age_seconds=90)


def test_monitor_is_opt_in_without_status_path():
    assert sentinel.credentials_ready(None, max_age_seconds=90)


@pytest.mark.asyncio
async def test_governance_fails_closed_with_retry_after_when_status_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        governance_main.settings,
        "google_adc_status_path",
        str(tmp_path / "missing-status.json"),
    )
    transport = httpx.ASGITransport(app=governance_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://governance") as client:
        response = await client.post(
            "/inspect",
            headers={"X-Internal-Token": governance_main.settings.internal_token},
            json={
                "text": "must not reach the pipeline",
                "tenant_id": "tenant",
                "user_id": "user",
                "model_id": "model",
                "routing_method": "direct",
            },
        )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.json() == {"detail": "PII inspection temporarily unavailable"}
    assert "must not reach" not in response.text


@pytest.mark.asyncio
async def test_liveness_does_not_depend_on_google_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(
        governance_main.settings,
        "google_adc_status_path",
        str(tmp_path / "missing-status.json"),
    )
    transport = httpx.ASGITransport(app=governance_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://governance") as client:
        response = await client.get("/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
