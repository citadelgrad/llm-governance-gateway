from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import get_session
from app.main import app
from app.settings import settings

VALID_BODY = {
    "tenant_id": "tenant-1",
    "user_id": "user-1",
    "event_type": "mcp_tool_call",
    "decision": "allow",
}


@pytest.fixture
def client(monkeypatch):
    async def _fake_write_mcp_audit_event(session, **kwargs):
        return None

    async def _fake_get_session():
        yield None

    monkeypatch.setattr(main_module.audit_module, "write_mcp_audit_event", _fake_write_mcp_audit_event)
    app.dependency_overrides[get_session] = _fake_get_session

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def test_endpoint_requires_token(client):
    resp = client.post("/v1/mcp/audit-event", json=VALID_BODY)

    assert resp.status_code == 403


def test_endpoint_rejects_bad_token(client):
    resp = client.post(
        "/v1/mcp/audit-event",
        headers={"X-Internal-Token": settings.internal_token + "-wrong"},
        json=VALID_BODY,
    )

    assert resp.status_code == 403


def test_endpoint_writes_audit_event_with_valid_token(client):
    resp = client.post(
        "/v1/mcp/audit-event",
        headers={"X-Internal-Token": settings.internal_token},
        json=VALID_BODY,
    )

    assert resp.status_code == 200
    assert "audit_id" in resp.json()


def test_endpoint_accepts_dlp_blocked_event(client):
    resp = client.post(
        "/v1/mcp/audit-event",
        headers={"X-Internal-Token": settings.internal_token},
        json={
            "tenant_id": "tenant-1",
            "user_id": "user-1",
            "event_type": "dlp_blocked",
            "decision": "block",
        },
    )

    assert resp.status_code == 200
    assert "audit_id" in resp.json()
