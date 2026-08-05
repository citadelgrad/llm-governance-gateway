output "developer_principal" {
  description = "Dedicated developer principal granted the narrow custom role."
  value       = var.developer_principal
}

output "dlp_service_account_email" {
  description = "Existing service account used for DLP calls."
  value       = data.google_service_account.dlp.email
}

output "token_minter_role_name" {
  description = "Project custom role containing only getAccessToken."
  value       = google_project_iam_custom_role.dlp_token_minter.name
}
