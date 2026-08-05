import os
import re
import subprocess
from pathlib import Path

import hcl2


ROOT = Path(__file__).resolve().parents[1]
TF_ROOT = ROOT / "infra" / "terraform" / "google-dlp-dev-access"


def _terraform_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(TF_ROOT.glob("*.tf"))
    )


def _terraform_resources() -> dict[tuple[str, str], dict[str, object]]:
    resources: dict[tuple[str, str], dict[str, object]] = {}
    for path in sorted(TF_ROOT.glob("*.tf")):
        with path.open(encoding="utf-8") as terraform_file:
            parsed = hcl2.load(terraform_file)
        for resource_group in parsed.get("resource", []):
            for resource_type, named_resources in resource_group.items():
                for resource_name, body in named_resources.items():
                    resources[
                        (resource_type.strip('"'), resource_name.strip('"'))
                    ] = body
    return resources


def _literal(value: object) -> object:
    if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if isinstance(value, list):
        return [_literal(item) for item in value]
    return value


def _expression(value: object) -> str:
    assert isinstance(value, str)
    return " ".join(value.split())


def test_terraform_root_uses_partial_remote_gcs_backend() -> None:
    source = _terraform_source()

    assert 'backend "gcs" {}' in source
    assert not list(TF_ROOT.glob("*.tfstate*"))
    assert (TF_ROOT / "backend.hcl.example").is_file()
    assert (TF_ROOT / ".terraform.lock.hcl").is_file()


def test_dedicated_principal_and_target_are_pinned() -> None:
    source = _terraform_source()

    assert (
        'var.developer_principal == "user:ai-gateway-dev@happyherbivore.com"'
        in source
    )
    assert "user:scott@happyherbivore.com" in source
    assert 'var.project_id == "prod-meal-mentor"' in source
    assert (
        'var.dlp_service_account_email == "llm-governance-dlp@prod-meal-mentor.iam.gserviceaccount.com"'
        in source
    )


def test_terraform_resource_and_permission_inventory_is_exact() -> None:
    source = _terraform_source()
    resources = _terraform_resources()

    assert set(resources) == {
        ("google_project_service", "dlp"),
        ("google_project_iam_custom_role", "dlp_token_minter"),
        ("google_service_account_iam_member", "developer_token_minter"),
        ("google_project_iam_member", "dlp_user"),
        ("google_service_account_iam_member", "legacy_admin_token_creator"),
        ("google_storage_bucket", "terraform_state"),
    }
    assert _literal(resources[("google_project_service", "dlp")]["service"]) == "dlp.googleapis.com"
    assert resources[("google_project_service", "dlp")]["disable_on_destroy"] is False
    assert _literal(
        resources[("google_project_iam_custom_role", "dlp_token_minter")][
            "permissions"
        ]
    ) == ["iam.serviceAccounts.getAccessToken"]

    developer_binding = resources[
        ("google_service_account_iam_member", "developer_token_minter")
    ]
    assert developer_binding["service_account_id"] == "${data.google_service_account.dlp.name}"
    assert developer_binding["role"] == "${google_project_iam_custom_role.dlp_token_minter.name}"
    assert developer_binding["member"] == "${var.developer_principal}"

    dlp_binding = resources[("google_project_iam_member", "dlp_user")]
    assert dlp_binding["project"] == "${var.project_id}"
    assert _literal(dlp_binding["role"]) == "roles/dlp.user"
    assert _literal(dlp_binding["member"]) == (
        "serviceAccount:${data.google_service_account.dlp.email}"
    )
    assert dlp_binding["depends_on"] == ["${google_project_service.dlp}"]

    legacy_binding = resources[
        ("google_service_account_iam_member", "legacy_admin_token_creator")
    ]
    assert _expression(legacy_binding["count"]) == (
        '${var.legacy_admin_removal_approval != '
        '"REMOVE_SCOTT_AFTER_AI_GATEWAY_DEV_LIVE_PROOF" ? 1 : 0}'
    )
    assert legacy_binding["service_account_id"] == "${data.google_service_account.dlp.name}"
    assert _literal(
        legacy_binding["role"]
    ) == "roles/iam.serviceAccountTokenCreator"
    assert _literal(legacy_binding["member"]) == "user:scott@happyherbivore.com"
    assert re.search(r'data\s+"google_service_account"\s+"dlp"', source)


def test_legacy_admin_removal_is_exact_and_fail_closed() -> None:
    source = _terraform_source()
    readme = (TF_ROOT / "README.md").read_text(encoding="utf-8")

    assert 'resource "google_service_account_iam_member" "legacy_admin_token_creator"' in source
    assert 'default     = "PRESERVE"' in source
    assert "REMOVE_SCOTT_AFTER_AI_GATEWAY_DEV_LIVE_PROOF" in source
    assert "var.legacy_admin_removal_approval !=" in source
    assert 'role               = "roles/iam.serviceAccountTokenCreator"' in source
    assert "data.google_service_account.dlp.name" in source
    assert readme.index("legacy_admin_token_creator[0]") < readme.index(
        "terraform plan -out="
    )


def test_state_bucket_bootstrap_is_hardened_and_becomes_terraform_managed() -> None:
    source = _terraform_source()
    bootstrap = (TF_ROOT / "bootstrap-state-bucket.sh").read_text(encoding="utf-8")

    assert 'resource "google_storage_bucket" "terraform_state"' in source
    assert "prevent_destroy = true" in source
    assert "uniform_bucket_level_access = true" in source
    assert 'public_access_prevention    = "enforced"' in source
    assert "versioning" in source
    assert "gcloud storage buckets create" in bootstrap
    assert "gcloud storage buckets update" in bootstrap
    assert "add-iam-policy-binding" not in bootstrap
    assert '"$project_id" != "prod-meal-mentor"' in bootstrap
    assert "^prod-meal-mentor-llm-governance-tfstate-" in bootstrap


def test_state_bucket_bootstrap_refuses_to_adopt_an_existing_bucket(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {calls}\nexit 0\n",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)

    result = subprocess.run(
        [
            str(TF_ROOT / "bootstrap-state-bucket.sh"),
            "prod-meal-mentor",
            "prod-meal-mentor-llm-governance-tfstate-existing",
        ],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "refusing to mutate or adopt" in result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "storage buckets describe gs://prod-meal-mentor-llm-governance-tfstate-existing --project=prod-meal-mentor"
    ]
