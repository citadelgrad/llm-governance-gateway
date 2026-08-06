from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "onboard.py"


def run_cli(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or ROOT,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=True,
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
      - gpt-5.6-luna
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
        "gpt-5.6-luna",
        "--claude-model",
        "claude-sonnet-4-6",
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout

    assert "Hermes" in output
    assert "Claude Code" in output
    assert "Codex" in output
    assert "Continue" in output
    assert "https://gateway.example.com/v1" in output
    assert "gpt-5.6-luna" in output
    assert "claude-sonnet-4-6" in output
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


def test_configure_continue_upserts_gateway_model_and_preserves_existing_config(tmp_path: Path) -> None:
    continue_config = tmp_path / ".continue" / "config.yaml"
    continue_config.parent.mkdir()
    continue_config.write_text(
        """
name: Main Config
version: 1.0.0
schema: v1
models:
  - name: Existing Model
    provider: ollama
    model: qwen2.5-coder
""".lstrip()
    )

    args = (
        "configure-continue",
        "--gateway-url",
        "https://gateway.example.com",
        "--api-key-env",
        "GATEWAY_API_KEY",
        "--model",
        "gpt-5.6-luna",
        "--config-path",
        str(continue_config),
    )
    env = {"GATEWAY_API_KEY": "gw_test_key_not_a_real_secret"}

    first = run_cli(*args, env=env)
    second = run_cli(*args, env=env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    config = yaml.safe_load(continue_config.read_text())
    assert config["name"] == "Main Config"
    assert config["models"][0]["name"] == "Existing Model"
    gateway_models = [
        model for model in config["models"] if model["name"] == "LLM Governance Gateway"
    ]
    assert gateway_models == [
        {
            "name": "LLM Governance Gateway",
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "apiBase": "https://gateway.example.com/v1",
            "apiKey": "gw_test_key_not_a_real_secret",
            "useResponsesApi": True,
            "roles": ["chat", "edit", "apply"],
        }
    ]
    assert continue_config.stat().st_mode & 0o777 == 0o600
    backup = continue_config.with_suffix(".yaml.gateway-backup")
    assert backup.exists()
    assert "Existing Model" in backup.read_text()
    assert "LLM Governance Gateway" not in backup.read_text()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert "gw_test_key_not_a_real_secret" not in first.stdout


def test_configure_continue_requires_key_environment_variable(tmp_path: Path) -> None:
    continue_config = tmp_path / ".continue" / "config.yaml"
    result = run_cli(
        "configure-continue",
        "--gateway-url",
        "http://localhost:18765",
        "--api-key-env",
        "MISSING_GATEWAY_KEY_FOR_TEST",
        "--config-path",
        str(continue_config),
    )

    assert result.returncode != 0
    assert "MISSING_GATEWAY_KEY_FOR_TEST" in result.stderr
    assert not continue_config.exists()
