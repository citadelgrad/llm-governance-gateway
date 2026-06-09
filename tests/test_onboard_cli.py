from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "onboard.py"


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def write_seed_config(config_dir: Path) -> None:
    config_dir.mkdir()
    (config_dir / "users.yaml").write_text("users: []\n")
    (config_dir / "tenants.yaml").write_text(
        """
tenants:
  - id: acme-corp
    name: Acme Corporation
    allowed_models:
      - gpt-4o-mini
    rate_limit: 1000
    pii_action: redact
    pii_redaction_notification: true
    default_provider: openai
    contact_email: admin@example.com
""".lstrip()
    )


def test_add_user_is_idempotent_and_preserves_provisioner_placeholder(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    write_seed_config(config_dir)

    first = run_cli(
        "add-user",
        "--config-dir",
        str(config_dir),
        "--user-id",
        "scott-laptop",
        "--tenant-id",
        "acme-corp",
        "--role",
        "tier1",
    )
    assert first.returncode == 0, first.stderr
    assert "scott-laptop" in first.stdout

    second = run_cli(
        "add-user",
        "--config-dir",
        str(config_dir),
        "--user-id",
        "scott-laptop",
        "--tenant-id",
        "acme-corp",
        "--role",
        "tier1",
    )
    assert second.returncode == 0, second.stderr
    assert "already exists" in second.stdout

    users = yaml.safe_load((config_dir / "users.yaml").read_text())["users"]
    assert users == [
        {
            "id": "scott-laptop",
            "tenant_id": "acme-corp",
            "roles": ["tier1"],
            "initial_key": "REPLACE_IN_PROVISIONER",
        }
    ]


def test_add_service_account_adds_service_role_without_deployment_specifics(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    write_seed_config(config_dir)

    result = run_cli(
        "add-service-account",
        "--config-dir",
        str(config_dir),
        "--account-id",
        "ci-release",
        "--tenant-id",
        "acme-corp",
        "--role",
        "tier1",
    )
    assert result.returncode == 0, result.stderr

    users = yaml.safe_load((config_dir / "users.yaml").read_text())["users"]
    assert users[0]["id"] == "svc-ci-release"
    assert users[0]["roles"] == ["service_account", "tier1"]
    forbidden_deployer = "kam" + "al"
    assert forbidden_deployer not in result.stdout.lower()


def test_agent_config_prints_all_supported_agents_without_embedding_secrets() -> None:
    result = run_cli(
        "agent-config",
        "--gateway-url",
        "https://gateway.example.com",
        "--api-key-env",
        "GATEWAY_API_KEY",
        "--model",
        "gpt-4o-mini",
        "--claude-model",
        "claude-3-5-sonnet",
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout

    assert "Hermes" in output
    assert "Claude Code" in output
    assert "Codex" in output
    assert "https://gateway.example.com/v1" in output
    assert "gpt-4o-mini" in output
    assert "claude-3-5-sonnet" in output
    assert "GATEWAY_API_KEY" in output
    assert 'wire_api = "responses"' in output
    assert 'model_provider = "llm-governance-gateway"' in output
    assert 'env_key = "GATEWAY_API_KEY"' in output
    assert 'requires_openai_auth = false' in output
    assert "[profiles.gateway]" in output
    assert "macOS/Linux" in output
    assert "Windows PowerShell" in output
    assert "$env:GATEWAY_API_KEY" in output
    assert "gw_" not in output
    forbidden_deployer = "kam" + "al"
    assert forbidden_deployer.title() not in output
    assert forbidden_deployer not in output
