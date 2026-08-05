#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  printf 'usage: %s PROJECT_ID BUCKET_NAME [LOCATION]\n' "$0" >&2
  exit 2
fi

project_id=$1
bucket_name=$2
location=${3:-US}
bucket_uri="gs://${bucket_name}"

if [[ "$project_id" != "prod-meal-mentor" ]]; then
  printf 'error: project must be prod-meal-mentor\n' >&2
  exit 2
fi

if [[ ! "$bucket_name" =~ ^prod-meal-mentor-llm-governance-tfstate-[a-z0-9-]+$ ]]; then
  printf 'error: bucket name must use the dedicated prod-meal-mentor-llm-governance-tfstate-... prefix\n' >&2
  exit 2
fi

if gcloud storage buckets describe "$bucket_uri" --project="$project_id" >/dev/null 2>&1; then
  printf 'error: bucket already exists; refusing to mutate or adopt it\n' >&2
  printf 'choose a new globally unique dedicated-state bucket name\n' >&2
  exit 1
fi

gcloud storage buckets create "$bucket_uri" \
  --project="$project_id" \
  --location="$location" \
  --uniform-bucket-level-access \
  --public-access-prevention

gcloud storage buckets update "$bucket_uri" \
  --project="$project_id" \
  --versioning \
  --uniform-bucket-level-access \
  --public-access-prevention

printf 'State bucket ready. Initialize the partial backend, then import it immediately:\n'
printf '  terraform init -backend-config=backend.hcl\n'
printf '  terraform import google_storage_bucket.terraform_state %s\n' "$bucket_name"
