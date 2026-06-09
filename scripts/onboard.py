#!/usr/bin/env python3
"""Operator onboarding helper for tenants, users, service accounts, and agent endpoint config."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).parent.parent
DEFAULT_CONFIG_DIR = ROOT / "config"
PLACEHOLDER_KEY = "REPLACE_IN_PROVISIONER"


def _load_yaml(path: Path, top_key: str) -> dict[str, Any]:
    if not path.exists():
        return {top_key: []}
    data = yaml.safe_load(path.read_text()) or {}
    if top_key not in data or data[top_key] is None:
        data[top_key] = []
    if not isinstance(data[top_key], list):
        raise SystemExit(f"{path} must contain a top-level {top_key}: list")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _tenant_exists(config_dir: Path, tenant_id: str) -> bool:
    tenants = _load_yaml(config_dir / "tenants.yaml", "tenants")["tenants"]
    return any(t.get("id") == tenant_id for t in tenants if isinstance(t, dict))


def _normal_roles(roles: list[str], *, service_account: bool = False) -> list[str]:
    result: list[str] = []
    if service_account:
        result.append("service_account")
    for role in roles or ["tier1"]:
        if role not in result:
            result.append(role)
    return result


def add_identity(
    *,
    config_dir: Path,
    identity_id: str,
    tenant_id: str,
    roles: list[str],
    service_account: bool = False,
) -> str:
    if not _tenant_exists(config_dir, tenant_id):
        raise SystemExit(f"Unknown tenant_id {tenant_id!r}; add it to {config_dir / 'tenants.yaml'} first")

    users_path = config_dir / "users.yaml"
    data = _load_yaml(users_path, "users")
    users = data["users"]

    for user in users:
        if isinstance(user, dict) and user.get("id") == identity_id:
            return f"{identity_id} already exists in {users_path}; no changes made."

    users.append(
        {
            "id": identity_id,
            "tenant_id": tenant_id,
            "roles": _normal_roles(roles, service_account=service_account),
            "initial_key": PLACEHOLDER_KEY,
        }
    )
    _write_yaml(users_path, data)
    noun = "service account" if service_account else "user"
    return f"Added {noun} {identity_id} to tenant {tenant_id}. Run `make provision` to create the database row and initial API key."


def _gateway_v1_url(gateway_url: str) -> str:
    normalized = gateway_url.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("--gateway-url must be an absolute http(s) URL")
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def render_agent_config(*, gateway_url: str, api_key_env: str, model: str, claude_model: str) -> str:
    base = gateway_url.rstrip("/")
    v1 = _gateway_v1_url(gateway_url)
    return f"""# Gateway endpoint
GATEWAY_URL={base}
# Store the actual key in {api_key_env}; do not paste plaintext keys into shell profiles.

## macOS/Linux
export GATEWAY_URL=\"{base}\"
export {api_key_env}=\"set-this-in-your-secret-manager\"

### Hermes
hermes config set model.provider custom
hermes config set model.base_url \"{v1}\"
hermes config set model.api_key \"${api_key_env}\"
hermes config set model.default \"{model}\"

### Claude Code
# Requires an Anthropic Messages-compatible gateway/shim exposing /v1/messages.
export ANTHROPIC_BASE_URL=\"{base}\"
export ANTHROPIC_AUTH_TOKEN=\"${api_key_env}\"
export ANTHROPIC_MODEL=\"{claude_model}\"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1

### Codex
export {api_key_env}=\"set-this-in-your-secret-manager\"
codex -p gateway \"Reply with gateway-ok only.\"

## Windows PowerShell
$env:GATEWAY_URL = \"{base}\"
$env:{api_key_env} = \"set-this-in-your-secret-manager\"

### Hermes
hermes config set model.provider custom
hermes config set model.base_url \"{v1}\"
hermes config set model.api_key \"$env:{api_key_env}\"
hermes config set model.default \"{model}\"

### Claude Code
$env:ANTHROPIC_BASE_URL = \"{base}\"
$env:ANTHROPIC_AUTH_TOKEN = \"$env:{api_key_env}\"
$env:ANTHROPIC_MODEL = \"{claude_model}\"
$env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = \"1\"

### Codex config file
# Add to ~/.codex/config.toml or %USERPROFILE%\\.codex\\config.toml.
# Uses the gateway's /v1/responses compatibility endpoint.
model = \"{model}\"
model_provider = \"llm-governance-gateway\"

[model_providers.llm-governance-gateway]
name = \"LLM Governance Gateway\"
base_url = \"{v1}\"
wire_api = \"responses\"
env_key = \"{api_key_env}\"
requires_openai_auth = false

[profiles.gateway]
model_provider = \"llm-governance-gateway\"
model = \"{model}\"
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_identity_args(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
        subparser.add_argument("--tenant-id", required=True)
        subparser.add_argument("--role", action="append", default=[])

    add_user = subparsers.add_parser("add-user", help="Add a human user to config/users.yaml")
    add_identity_args(add_user)
    add_user.add_argument("--user-id", required=True)

    add_service = subparsers.add_parser(
        "add-service-account", help="Add a service account to config/users.yaml"
    )
    add_identity_args(add_service)
    add_service.add_argument("--account-id", required=True)

    agent_config = subparsers.add_parser(
        "agent-config", help="Print endpoint configuration snippets for supported agents"
    )
    agent_config.add_argument("--gateway-url", required=True)
    agent_config.add_argument("--api-key-env", default="GATEWAY_API_KEY")
    agent_config.add_argument("--model", default="gpt-4o-mini")
    agent_config.add_argument("--claude-model", default="claude-3-5-sonnet")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "add-user":
        print(
            add_identity(
                config_dir=args.config_dir,
                identity_id=args.user_id,
                tenant_id=args.tenant_id,
                roles=args.role,
            )
        )
        return 0
    if args.command == "add-service-account":
        account_id = args.account_id if args.account_id.startswith("svc-") else f"svc-{args.account_id}"
        print(
            add_identity(
                config_dir=args.config_dir,
                identity_id=account_id,
                tenant_id=args.tenant_id,
                roles=args.role,
                service_account=True,
            )
        )
        return 0
    if args.command == "agent-config":
        print(render_agent_config(gateway_url=args.gateway_url, api_key_env=args.api_key_env, model=args.model, claude_model=args.claude_model))
        return 0
    raise SystemExit(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
