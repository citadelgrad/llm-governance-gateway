from __future__ import annotations

import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from mcpproxy.app.config import settings
from mcpproxy.app.opa_client import OpaClient

# ai-gateway-d7w: config/users.yaml's tenant IDs (acme-corp, internal,
# local-integration) didn't match policies/mcp/authz_test.rego's rego-literal
# example tenant ID, so the rego suite never exercised a real config-sourced
# tenant_id end-to-end. authz_test.rego's literal was realigned to
# "acme-corp" (a real config/users.yaml tenant) alongside this test, which
# sources that same tenant_id from config/users.yaml itself - not a second
# rego-only literal - and drives it through a real OPA process evaluating
# the actual policies/mcp/authz.rego file over HTTP, via the same OpaClient
# class every other mcpproxy test mocks out.
#
# Note: policies/mcp/authz.rego's entitlement matrix is keyed by role only
# (mcp-role:github-write, mcp-role:read-only) - tenant_id is not itself
# consulted by its `allow` rules, so this test cannot show tenant_id
# *changing* the decision. What it proves is the full request path (a real
# tenant_id, sourced from the real config file, flowing through the real
# OpaClient into a real OPA server loaded with the real policy) behaves the
# way authz_test.rego's equivalent allow/deny cases say it should.
#
# config/users.yaml's own roles (admin/tier1/tier2) are the LLM Gateway
# Proxy's tenant-tier vocabulary (see policies/llm/authz.rego) - a different
# namespace from the MCP entitlement matrix's Zitadel-issued project roles
# used here. Only the tenant_id dimension is config-sourced; the roles below
# are real values from the MCP-role vocabulary the entitlement matrix keys
# on (docs/auth-architecture.md, "Scope & entitlement model").

REPO_ROOT = Path(__file__).resolve().parents[2]
USERS_YAML = REPO_ROOT / "config" / "users.yaml"
POLICIES_DIR = REPO_ROOT / "policies"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="requires Docker to run a real OPA server against policies/mcp/authz.rego",
)


def _load_tenant_id(user_id: str) -> str:
    """Read a real user's tenant_id straight out of config/users.yaml.

    A small line-oriented reader, not a general YAML parser: config/users.yaml
    is a flat list of scalar/list fields written only by scripts/provision.py
    and scripts/onboard.py, so this avoids adding a YAML dependency to
    mcpproxy's declared dependencies just to read a handful of "tenant_id:
    <value>" lines for this one test.
    """
    current_id = None
    for line in USERS_YAML.read_text().splitlines():
        id_match = re.match(r"-\s*id:\s*(\S+)", line)
        if id_match:
            current_id = id_match.group(1)
            continue
        if current_id == user_id:
            tenant_match = re.match(r"\s+tenant_id:\s*(\S+)", line)
            if tenant_match:
                return tenant_match.group(1)
    raise AssertionError(f"config/users.yaml has no user {user_id!r} with a tenant_id")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def opa_url():
    """Start a real, throwaway OPA server loaded with the actual policies/
    directory (same "opa run --server /policies" shape as docker-compose.yml's
    ingress `opa` service), on a dynamically-picked free port so this never
    collides with a locally-running dev stack. Tears the container down on
    fixture teardown regardless of test outcome.
    """
    port = _free_port()
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "-p",
            f"127.0.0.1:{port}:8181",
            "-v",
            f"{POLICIES_DIR}:/policies:ro",
            "openpolicyagent/opa:0.68.0-static",
            "run",
            "--server",
            "--addr",
            "0.0.0.0:8181",
            "/policies",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    container_id = result.stdout.strip()
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{base_url}/health", timeout=1.0)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError as exc:
                last_exc = exc
            time.sleep(0.3)
        else:
            raise RuntimeError(f"OPA container did not become healthy in time: {last_exc}")
        yield base_url
    finally:
        subprocess.run(["docker", "stop", container_id], capture_output=True)


async def test_allow_decision_for_config_sourced_tenant(opa_url, monkeypatch):
    tenant_id = _load_tenant_id("admin-acme")

    monkeypatch.setattr(settings, "opa_sidecar_url", opa_url)

    async with httpx.AsyncClient() as http_client:
        opa_client = OpaClient(http_client)

        allowed = await opa_client.check_tool_call(
            principal={
                "user_id": "admin-acme",
                "tenant_id": tenant_id,
                "roles": ["mcp-role:github-write"],
            },
            actor={},
            tool={
                "server": "github-mcp",
                "name": "create_pr",
                "arguments": {"repo": "org/name", "base": "main"},
            },
            context={
                "environment": "prod",
                "resource": "repo:org/name",
                "prior_calls_this_session": 1,
            },
        )
        assert allowed is True


async def test_deny_decision_for_config_sourced_tenant(opa_url, monkeypatch):
    tenant_id = _load_tenant_id("admin-acme")

    monkeypatch.setattr(settings, "opa_sidecar_url", opa_url)

    async with httpx.AsyncClient() as http_client:
        opa_client = OpaClient(http_client)

        allowed = await opa_client.check_tool_call(
            principal={
                "user_id": "admin-acme",
                "tenant_id": tenant_id,
                "roles": ["mcp-role:read-only"],
            },
            actor={},
            tool={
                "server": "github-mcp",
                "name": "create_pr",
                "arguments": {"repo": "org/name", "base": "main"},
            },
            context={
                "environment": "prod",
                "resource": "repo:org/name",
                "prior_calls_this_session": 1,
            },
        )
        assert allowed is False
