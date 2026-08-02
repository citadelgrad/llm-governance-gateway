import gzip
import io
import json
import tarfile
import unicodedata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.entitlements import EntitlementsError, _build_bundle, _extract_entitlements, get_bundle

REAL_REGO_PATH = str(Path(__file__).resolve().parents[2] / "policies" / "mcp" / "authz.rego")


def _untar(bundle_bytes: bytes) -> dict[str, bytes]:
    tar_bytes = gzip.decompress(bundle_bytes)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        return {member.name: tar.extractfile(member).read() for member in tar.getmembers()}


def test_extract_entitlements_reads_real_authz_rego():
    with open(REAL_REGO_PATH) as f:
        rego_text = f.read()

    result = _extract_entitlements(rego_text)

    assert result == {
        "mcp-role:github-write": [
            {
                "server": "github-mcp",
                "tool": "create_pr",
                "resource_pattern": "repo:org/*",
                "tenant_id": "tenant_acme",
            },
        ],
        "mcp-role:read-only": [
            {
                "server": "github-mcp",
                "tool": "list_prs",
                "resource_pattern": None,
                "tenant_id": "tenant_acme",
            },
        ],
    }


def test_extract_entitlements_handles_trailing_commas():
    rego_text = """
    package mcp.authz

    entitlements := {
        "role-a": [
            {"server": "s", "tool": "t", "resource_pattern": null},
        ],
    }
    """

    result = _extract_entitlements(rego_text)

    assert result == {"role-a": [{"server": "s", "tool": "t", "resource_pattern": None}]}


def test_extract_entitlements_missing_marker_raises():
    with pytest.raises(EntitlementsError, match="entitlements"):
        _extract_entitlements("package mcp.authz\n\nallow := false\n")


def test_extract_entitlements_unbalanced_braces_raises():
    with pytest.raises(EntitlementsError, match="unbalanced"):
        _extract_entitlements("entitlements := {\n  \"a\": [1, 2]\n")


def test_extract_entitlements_invalid_json_raises():
    with pytest.raises(EntitlementsError, match="not valid JSON"):
        _extract_entitlements("entitlements := {not: json}")


def test_extract_entitlements_ignores_braces_inside_strings():
    rego_text = 'entitlements := {"role": "value with { and } inside"}'

    result = _extract_entitlements(rego_text)

    assert result == {"role": "value with { and } inside"}


def test_build_bundle_is_deterministic():
    data = {"role": [{"server": "s", "tool": "t", "resource_pattern": None}]}

    first = _build_bundle(data, revision="abc123")
    second = _build_bundle(data, revision="abc123")

    assert first == second


def test_build_bundle_shape():
    data = {"role": [{"server": "s", "tool": "t", "resource_pattern": None}]}

    bundle_bytes = _build_bundle(data, revision="abc123")
    files = _untar(bundle_bytes)

    assert set(files) == {"data.json", ".manifest"}
    assert json.loads(files["data.json"]) == {"mcp": {"authz": {"entitlements": data}}}
    manifest = json.loads(files[".manifest"])
    assert manifest["revision"] == "abc123"
    assert manifest["roots"] == ["mcp/authz/entitlements"]


async def test_get_bundle_reads_real_file():
    bundle_bytes = await get_bundle(REAL_REGO_PATH)
    files = _untar(bundle_bytes)

    data = json.loads(files["data.json"])
    assert "mcp-role:github-write" in data["mcp"]["authz"]["entitlements"]


async def test_get_bundle_idempotent_when_file_unchanged(tmp_path):
    rego_file = tmp_path / "authz.rego"
    rego_file.write_text('entitlements := {"role": []}')

    first = await get_bundle(str(rego_file))
    second = await get_bundle(str(rego_file))

    assert first == second


async def test_get_bundle_reflects_live_edit(tmp_path):
    rego_file = tmp_path / "authz.rego"
    rego_file.write_text('entitlements := {"role-a": []}')

    before = await get_bundle(str(rego_file))

    rego_file.write_text('entitlements := {"role-b": []}')
    after = await get_bundle(str(rego_file))

    assert before != after
    after_data = json.loads(_untar(after)["data.json"])
    assert after_data["mcp"]["authz"]["entitlements"] == {"role-b": []}


async def test_get_bundle_normalizes_non_nfc_resource_pattern_to_nfc(tmp_path):
    """ai-gateway-vci: a resource_pattern authored in a non-NFC Unicode form
    (e.g. NFD-decomposed accents) must reach the OPA bundle in NFC, matching
    the form mcpproxy's _normalize_context already applies to
    input.context.resource - otherwise an equivalent request could
    false-deny depending only on how the pattern happened to be typed."""
    nfc_pattern = "repo:caf\u00e9/*"  # single precomposed "e with acute accent"
    nfd_pattern = unicodedata.normalize("NFD", nfc_pattern)  # "e" + combining acute accent
    assert nfd_pattern != nfc_pattern

    rego_file = tmp_path / "authz.rego"
    rego_file.write_text(
        'entitlements := {"role": [{"server": "s", "tool": "t", '
        f'"resource_pattern": "{nfd_pattern}"}}]}}'
    )

    bundle_bytes = await get_bundle(str(rego_file))
    data = json.loads(_untar(bundle_bytes)["data.json"])

    assert (
        data["mcp"]["authz"]["entitlements"]["role"][0]["resource_pattern"] == nfc_pattern
    )


async def test_get_bundle_nonexistent_path_raises():
    with pytest.raises(EntitlementsError):
        await get_bundle("/nonexistent/authz.rego")


async def test_get_bundle_malformed_file_raises(tmp_path):
    rego_file = tmp_path / "authz.rego"
    rego_file.write_text("package mcp.authz\n\nallow := false\n")

    with pytest.raises(EntitlementsError):
        await get_bundle(str(rego_file))


# ─── Endpoint-level tests ────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.main import app
    from app.settings import settings

    rego_file = tmp_path / "authz.rego"
    rego_file.write_text('entitlements := {"role": [{"server": "s", "tool": "t", "resource_pattern": null}]}')
    monkeypatch.setattr(settings, "entitlements_rego_path", str(rego_file))

    with TestClient(app) as test_client:
        yield test_client


def test_endpoint_requires_token(client):
    resp = client.get("/v1/mcp/entitlements-bundle")

    assert resp.status_code == 403


def test_endpoint_rejects_bad_token(client):
    from app.settings import settings

    resp = client.get(
        "/v1/mcp/entitlements-bundle",
        headers={"X-Internal-Token": settings.internal_token + "-wrong"},
    )

    assert resp.status_code == 403


def test_endpoint_returns_bundle_with_valid_token(client):
    from app.settings import settings

    resp = client.get(
        "/v1/mcp/entitlements-bundle",
        headers={"X-Internal-Token": settings.internal_token},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    files = _untar(resp.content)
    data = json.loads(files["data.json"])
    assert data["mcp"]["authz"]["entitlements"] == {
        "role": [{"server": "s", "tool": "t", "resource_pattern": None}]
    }


def test_endpoint_requires_no_body_params(client):
    from app.settings import settings

    # No model_id/routing_method anywhere - a bare GET is sufficient.
    resp = client.get(
        "/v1/mcp/entitlements-bundle",
        headers={"X-Internal-Token": settings.internal_token},
    )

    assert resp.status_code == 200


def test_endpoint_error_on_malformed_rego(tmp_path, monkeypatch):
    from app.main import app
    from app.settings import settings

    rego_file = tmp_path / "authz.rego"
    rego_file.write_text("package mcp.authz\n\nallow := false\n")
    monkeypatch.setattr(settings, "entitlements_rego_path", str(rego_file))

    with TestClient(app) as test_client:
        resp = test_client.get(
            "/v1/mcp/entitlements-bundle",
            headers={"X-Internal-Token": settings.internal_token},
        )

    assert resp.status_code == 500
