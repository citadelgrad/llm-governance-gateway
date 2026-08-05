variable "project_id" {
  description = "Google Cloud project containing the existing DLP service account."
  type        = string
  default     = "prod-meal-mentor"

  validation {
    condition     = var.project_id == "prod-meal-mentor"
    error_message = "project_id must remain prod-meal-mentor for this isolated root."
  }
}

variable "developer_principal" {
  description = "Dedicated Cloud Identity user principal, for example user:ai-gateway-dev@happyherbivore.com."
  type        = string

  validation {
    condition     = var.developer_principal == "user:ai-gateway-dev@happyherbivore.com"
    error_message = "developer_principal must remain user:ai-gateway-dev@happyherbivore.com for this production-specific root."
  }
}

variable "dlp_service_account_email" {
  description = "Email of the existing keyless DLP service account."
  type        = string
  default     = "llm-governance-dlp@prod-meal-mentor.iam.gserviceaccount.com"

  validation {
    condition     = var.dlp_service_account_email == "llm-governance-dlp@prod-meal-mentor.iam.gserviceaccount.com"
    error_message = "dlp_service_account_email must remain the existing llm-governance-dlp service account in prod-meal-mentor."
  }
}

variable "state_bucket_name" {
  description = "Dedicated GCS backend bucket bootstrapped before terraform init and then imported into this root."
  type        = string

  validation {
    condition     = can(regex("^prod-meal-mentor-llm-governance-tfstate-[a-z0-9-]+$", var.state_bucket_name))
    error_message = "state_bucket_name must use the dedicated prod-meal-mentor-llm-governance-tfstate-... prefix."
  }
}

variable "state_bucket_location" {
  description = "Location of the dedicated Terraform state bucket."
  type        = string
  default     = "US"
}

variable "legacy_admin_removal_approval" {
  description = "Fail-closed approval: preserve the imported admin member by default; use the exact removal phrase only after recorded dedicated-principal live proof."
  type        = string
  default     = "PRESERVE"

  validation {
    condition = contains([
      "PRESERVE",
      "REMOVE_SCOTT_AFTER_AI_GATEWAY_DEV_LIVE_PROOF",
    ], var.legacy_admin_removal_approval)
    error_message = "legacy_admin_removal_approval must be PRESERVE or the exact reviewed post-proof removal phrase."
  }
}
