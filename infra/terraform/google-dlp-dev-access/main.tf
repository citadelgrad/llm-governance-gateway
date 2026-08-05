provider "google" {
  project = var.project_id
}

data "google_service_account" "dlp" {
  account_id = split("@", var.dlp_service_account_email)[0]
}

resource "google_project_service" "dlp" {
  project            = var.project_id
  service            = "dlp.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_iam_custom_role" "dlp_token_minter" {
  project     = var.project_id
  role_id     = "llmGovernanceDlpTokenMinter"
  title       = "LLM Governance DLP Token Minter"
  description = "Allows a dedicated developer to mint short-lived access tokens for the exact DLP service account."
  permissions = ["iam.serviceAccounts.getAccessToken"]
}

resource "google_service_account_iam_member" "developer_token_minter" {
  service_account_id = data.google_service_account.dlp.name
  role               = google_project_iam_custom_role.dlp_token_minter.name
  member             = var.developer_principal
}

resource "google_project_iam_member" "dlp_user" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:${data.google_service_account.dlp.email}"

  depends_on = [google_project_service.dlp]
}

# Migration-only resource. Import the existing exact-service-account binding
# under the default PRESERVE action. Only the explicit post-proof approval phrase
# removes this resource from configuration and plans deletion of that one member.
resource "google_service_account_iam_member" "legacy_admin_token_creator" {
  count = var.legacy_admin_removal_approval != "REMOVE_SCOTT_AFTER_AI_GATEWAY_DEV_LIVE_PROOF" ? 1 : 0

  service_account_id = data.google_service_account.dlp.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "user:scott@happyherbivore.com"
}

resource "google_storage_bucket" "terraform_state" {
  name                        = var.state_bucket_name
  project                     = var.project_id
  location                    = var.state_bucket_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}
