# Isolated Google DLP developer access

This standalone Terraform root manages the narrow developer-to-DLP impersonation path in `prod-meal-mentor`. It references the existing keyless `llm-governance-dlp` service account; it does not create service-account keys, Workspace users, Cloud Identity users, or Workspace Trusted-app policy.

The proposed source principal is `user:ai-gateway-dev@happyherbivore.com`. Confirm that account exists and is excluded from admin/IAM-bearing groups before any apply. The variable validation rejects the everyday administrator `user:scott@happyherbivore.com`.

## State bootstrap

The root has a partial GCS backend. Never run it with local state and never use an application-data bucket for state.

1. Choose a new dedicated, globally unique bucket name beginning with `prod-meal-mentor-llm-governance-tfstate-`. Authenticate with the approved bootstrap administrator and create the bucket without changing IAM. The helper fails closed if the bucket already exists; it never mutates or adopts a pre-existing bucket:

   ```bash
   ./bootstrap-state-bucket.sh prod-meal-mentor "$STATE_BUCKET" US
   cp backend.hcl.example backend.hcl
   # Replace the bucket placeholder in backend.hcl.
   ```

2. Initialize the remote backend and immediately import the bootstrapped bucket so Terraform owns its permanent configuration:

   ```bash
   terraform init -backend-config=backend.hcl
   terraform import \
     -var='developer_principal=user:ai-gateway-dev@happyherbivore.com' \
     -var="state_bucket_name=$STATE_BUCKET" \
     google_storage_bucket.terraform_state "$STATE_BUCKET"
   ```

3. Before the first plan, inventory all TokenCreator members read-only and
   import the exact existing administrator member. Never use an authoritative
   binding or clobber unrelated principals:

   ```bash
   gcloud iam service-accounts get-iam-policy \
     llm-governance-dlp@prod-meal-mentor.iam.gserviceaccount.com \
     --project prod-meal-mentor \
     --flatten='bindings[].members' \
     --filter='bindings.role:roles/iam.serviceAccountTokenCreator' \
     --format='table(bindings.role,bindings.members)'

   terraform import \
     -var='developer_principal=user:ai-gateway-dev@happyherbivore.com' \
     -var="state_bucket_name=$STATE_BUCKET" \
     'google_service_account_iam_member.legacy_admin_token_creator[0]' \
     'projects/prod-meal-mentor/serviceAccounts/llm-governance-dlp@prod-meal-mentor.iam.gserviceaccount.com roles/iam.serviceAccountTokenCreator user:scott@happyherbivore.com'
   ```

`backend.hcl`, local tfvars, `.terraform/`, plans, and state are ignored. The managed bucket has uniform bucket-level access, public-access prevention, object versioning, and `prevent_destroy`.

## Validate and apply the narrow grant

Copy `terraform.tfvars.example` to an ignored `terraform.tfvars`, fill the state bucket, and confirm the dedicated principal. Then:

```bash
terraform fmt -check -recursive
terraform validate
# From the repository root:
make terraform-policy-test
terraform plan -out=google-dlp-dev-access.tfplan
terraform show google-dlp-dev-access.tfplan
terraform apply google-dlp-dev-access.tfplan
```

The reviewed plan must contain only:

- `dlp.googleapis.com` enabled without disable-on-destroy;
- the existing DLP service account lookup;
- a project custom role containing only `iam.serviceAccounts.getAccessToken`;
- that custom role bound additively to the dedicated user on the exact service account;
- `roles/dlp.user` bound additively to the DLP service account; and
- management of the hardened state bucket; and
- no change to the imported `legacy_admin_token_creator[0]` member.

Reject any plan containing a service-account key, owner/editor role, project-wide TokenCreator grant, authoritative IAM binding, or unrelated principal removal.

## Prove the dedicated identity before removing admin access

Create ADC while signed in as the dedicated user, then use the existing Keychain workflow and live controls:

```bash
make google-adc-login
make google-adc-keychain-store
make google-adc-preflight
make smoke-google-dlp
make smoke-live
```

Record the browser-selected source identity and use Cloud Audit Logs for the
`GenerateAccessToken` call to confirm its `principalEmail` is
`ai-gateway-dev@happyherbivore.com`:

```bash
gcloud logging read \
  'protoPayload.serviceName="iamcredentials.googleapis.com" AND protoPayload.methodName="google.iam.credentials.v1.IAMCredentials.GenerateAccessToken" AND protoPayload.authenticationInfo.principalEmail="ai-gateway-dev@happyherbivore.com"' \
  --project prod-meal-mentor \
  --freshness=30m \
  --limit=10 \
  --format='table(timestamp,protoPayload.authenticationInfo.principalEmail)'
```

If the relevant Data Access log is unavailable, stop rather than assuming the
browser account was the token source. The ADC preflight proves the effective
service account, not the source user. Do not remove the old administrator
binding until those checks prove the dedicated source principal can mint the
expected service-account token and the live DLP/gateway controls pass.

## Remove only the old member

The administrator member was imported before the first plan under
`legacy_admin_removal_approval = "PRESERVE"`. Every pre-proof plan must preserve
`legacy_admin_token_creator[0]` with no IAM change. If the member is absent or a
plan proposes creating it, stop and reconcile the live IAM inventory before
continuing.

After dedicated-principal live proof, set:

```hcl
legacy_admin_removal_approval = "REMOVE_SCOTT_AFTER_AI_GATEWAY_DEV_LIVE_PROOF"
```

Save and review a plan that deletes exactly
`legacy_admin_token_creator[0]`, then apply that saved plan. Omitting local
tfvars fails closed to `PRESERVE`; it cannot approve deletion. Do not use
`gcloud` for the IAM mutation.

Finally, read IAM back with the command above and confirm:

- the dedicated principal has only the custom `getAccessToken` role on the exact DLP service account;
- `user:scott@happyherbivore.com` no longer has TokenCreator there;
- unrelated service-account IAM members remain; and
- the DLP service account retains `roles/dlp.user`.

Workspace user lifecycle and Trusted-app policy are deliberately outside this root and require a separate Workspace-administrator boundary.
