.PHONY: up down restart watch status logs migrate lint test test-integration smoke-live smoke-google-dlp google-adc-login google-adc-renew google-adc-keychain-renew google-adc-preflight google-adc-keychain-store google-adc-keychain-materialize terraform-fmt terraform-validate terraform-policy-test terraform-check opa-test provision onboard-help rotate-partitions demo help

JWT_SECRET ?= local-dev-jwt-secret-for-compose-tests-only
GOVERNANCE_INTERNAL_TOKEN ?= local-dev-governance-token
PSEUDONYM_HMAC_KEY ?= local-dev-pseudonym-hmac-key-for-compose-tests-only
PII_BACKEND ?= presidio
GATEWAY_PROXY_PORT ?= 18765
GATEWAY_POSTGRES_PORT ?= 15433
GATEWAY_MCPPROXY_PORT ?= 18766
GATEWAY_BASE_URL ?= http://localhost:$(GATEWAY_PROXY_PORT)
DATABASE_URL ?= postgresql://gateway:gateway@localhost:$(GATEWAY_POSTGRES_PORT)/gateway
export JWT_SECRET GOVERNANCE_INTERNAL_TOKEN PSEUDONYM_HMAC_KEY PII_BACKEND GATEWAY_PROXY_PORT GATEWAY_POSTGRES_PORT GATEWAY_MCPPROXY_PORT GATEWAY_BASE_URL DATABASE_URL
DIRENV ?= direnv exec $(CURDIR)

## Service lifecycle
up:
	$(DIRENV) docker compose up -d --wait

down:
	$(DIRENV) docker compose down

restart:
	$(MAKE) down
	$(MAKE) up

watch:
	$(DIRENV) docker compose watch

status:
	$(DIRENV) docker compose ps

logs:
	$(DIRENV) docker compose logs -f

## Database
migrate:
	$(DIRENV) docker compose run --rm migrate
	$(DIRENV) docker compose run --rm proxy-migrate

## Code quality
# --frozen: use each service's committed uv.lock exactly as-is, never relock.
# Without it, `uv --no-config run ...` disables project-level [tool.uv]
# config too (not just personal ~/.config/uv/uv.toml), which can trigger a
# silent relock that strips the pinned exclude-newer [options] block back
# out of uv.lock (ai-gateway-xv6u).
lint:
	cd proxy && uv --no-config run --frozen --extra dev ruff check . && uv --no-config run --frozen --extra dev ty check app
	cd governance && uv --no-config run --frozen --extra dev ruff check app && uv --no-config run --frozen --extra dev ty check app
	cd mcpproxy && uv --no-config run --frozen --extra dev ruff check . && uv --no-config run --frozen --extra dev ty check app

test:
	cd proxy && uv --no-config run --frozen --extra dev python -m pytest tests/
	cd governance && PII_BACKEND=presidio uv --no-config run --frozen --extra dev python -m pytest tests/
	cd mcpproxy && uv --no-config run --frozen --extra dev python -m pytest tests/
	uv --no-config run --with pyyaml --with bcrypt --with httpx --with pytest --with python-hcl2 python -m pytest tests/ --ignore=tests/integration

test-integration:
	MOCK_PROVIDERS=true $(MAKE) up
	$(MAKE) provision
	cd proxy && INTEGRATION_TEST=1 uv --no-config run pytest ../tests/integration/ -v

smoke-live:
	$(DIRENV) uv --no-config run --with httpx scripts/live_smoke.py

smoke-google-dlp:
	cd governance && $(DIRENV) uv --no-config run python ../scripts/live_google_dlp.py

google-adc-login:
	$(DIRENV) bash -eu -c ': "$${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"; : "$${GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT:?set GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT}"; gcloud auth application-default login --project="$$GOOGLE_CLOUD_PROJECT" --impersonate-service-account="$$GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT"'

google-adc-preflight:
	cd governance && $(DIRENV) uv --no-config run python ../scripts/google_adc_preflight.py

google-adc-keychain-store:
	$(DIRENV) bash -eu -c ': "$${GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT:?set GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT}"; config_dir="$$(gcloud info --format="value(config.paths.global_config_dir)")"; scripts/google_adc_keychain.py store --source "$$config_dir/application_default_credentials.json" --expected-service-account "$$GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT" --remove-source'

google-adc-keychain-materialize:
	$(DIRENV) bash -eu -c ': "$${GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT:?set GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT}"; scripts/google_adc_keychain.py materialize --expected-service-account "$$GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT"'

google-adc-renew:
	$(MAKE) google-adc-login
	$(DIRENV) bash -eu -c 'config_dir="$$(gcloud info --format="value(config.paths.global_config_dir)")"; adc="$$config_dir/application_default_credentials.json"; test -f "$$adc"; GOOGLE_APPLICATION_CREDENTIALS_HOST="$$adc" GOOGLE_APPLICATION_CREDENTIALS="$$adc" docker compose up -d --wait --force-recreate google-credential-sentinel governance'

google-adc-keychain-renew:
	$(MAKE) google-adc-login
	$(MAKE) google-adc-keychain-store
	$(DIRENV) bash -eu -c 'adc="$$(scripts/google_adc_keychain.py materialize --expected-service-account "$$GOOGLE_DLP_EXPECTED_SERVICE_ACCOUNT")"; test -f "$$adc"; GOOGLE_APPLICATION_CREDENTIALS_HOST="$$adc" GOOGLE_APPLICATION_CREDENTIALS="$$adc" docker compose up -d --wait --force-recreate google-credential-sentinel governance'

## Google DLP developer-access Terraform
terraform-fmt:
	terraform -chdir=infra/terraform/google-dlp-dev-access fmt -check -recursive

terraform-validate:
	terraform -chdir=infra/terraform/google-dlp-dev-access init -backend=false -input=false -lockfile=readonly
	terraform -chdir=infra/terraform/google-dlp-dev-access validate

terraform-policy-test:
	uv --no-config run --with pytest --with python-hcl2 python -m pytest tests/test_google_dlp_terraform.py

terraform-check: terraform-fmt terraform-validate terraform-policy-test

## OPA policy tests
opa-test:
	docker compose run --rm opa test /policies -v

## Provisioning
provision:
	$(DIRENV) uv --no-config run --with psycopg2-binary --with pyyaml --with bcrypt scripts/provision.py

onboard-help:
	uv --no-config run --with pyyaml scripts/onboard.py --help

rotate-partitions:
	cd governance && uv --no-config run python ../scripts/rotate_partitions.py

## Demo
demo:
	MOCK_PROVIDERS=true $(MAKE) up
	$(MAKE) provision
	MOCK_PROVIDERS=true uv --no-config run --with httpx --with 'python-jose[cryptography]' scripts/demo.py

## Help
help:
	@echo "Available targets:"
	@echo "  up                  Start all services (detached)"
	@echo "  down                Stop all services"
	@echo "  restart             Restart all services"
	@echo "  watch               Auto-restart only services affected by code changes"
	@echo "  status              Show service status"
	@echo "  logs                Follow service logs"
	@echo "  migrate             Run governance and proxy database migrations"
	@echo "  lint                Run ruff + ty on both services"
	@echo "  test                Run pytest on both services"
	@echo "  test-integration    Run Docker Compose smoke tests (requires make up)"
	@echo "  smoke-live          Test live gateway/provider protocols (uses provider tokens)"
	@echo "  smoke-google-dlp    Test Google DLP with ADC (uses billable API calls)"
	@echo "  google-adc-login    Create local impersonated ADC (interactive)"
	@echo "  google-adc-renew    Reauthorize plain gcloud ADC and recreate ADC consumers"
	@echo "  google-adc-keychain-renew Reauthorize Keychain ADC and recreate consumers"
	@echo "  google-adc-preflight Verify ADC identity and refresh without a DLP call"
	@echo "  google-adc-keychain-store Store impersonated ADC in macOS Keychain"
	@echo "  google-adc-keychain-materialize Materialize Keychain ADC for Compose"
	@echo "  terraform-check     Validate isolated Google DLP developer-access Terraform"
	@echo "  opa-test            Run OPA policy tests"
	@echo "  provision           Run IaC provisioner (idempotent)"
	@echo "  onboard-help        Show user/service-account onboarding CLI help"
	@echo "  rotate-partitions   Rotate audit_log partitions (runs nightly on configured scheduler)"
	@echo "  demo                Run 6 governance scenarios (make up + provision + demo.py)"

