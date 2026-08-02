.PHONY: up down restart status logs migrate lint test test-integration smoke-live smoke-google-dlp opa-test provision onboard-help rotate-partitions demo help

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

status:
	$(DIRENV) docker compose ps

logs:
	$(DIRENV) docker compose logs -f

## Database
migrate:
	$(DIRENV) docker compose run --rm migrate

## Code quality
lint:
	cd proxy && uv --no-config run --extra dev ruff check . && uv --no-config run --extra dev ty check app
	cd governance && uv --no-config run --extra dev ruff check app && uv --no-config run --extra dev ty check app
	cd mcpproxy && uv --no-config run --extra dev ruff check . && uv --no-config run --extra dev ty check app

test:
	cd proxy && uv --no-config run --extra dev python -m pytest tests/
	cd governance && PII_BACKEND=presidio uv --no-config run --extra dev python -m pytest tests/
	cd mcpproxy && uv --no-config run --extra dev python -m pytest tests/
	uv --no-config run --with pyyaml --with bcrypt --with pytest python -m pytest tests/ --ignore=tests/integration

test-integration:
	MOCK_PROVIDERS=true $(MAKE) up
	$(MAKE) provision
	cd proxy && INTEGRATION_TEST=1 uv --no-config run pytest ../tests/integration/ -v

smoke-live:
	$(DIRENV) uv --no-config run --with httpx scripts/live_smoke.py

smoke-google-dlp:
	cd governance && $(DIRENV) uv --no-config run python ../scripts/live_google_dlp.py

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
	@echo "  status              Show service status"
	@echo "  logs                Follow service logs"
	@echo "  migrate             Run database migrations"
	@echo "  lint                Run ruff + ty on both services"
	@echo "  test                Run pytest on both services"
	@echo "  test-integration    Run Docker Compose smoke tests (requires make up)"
	@echo "  smoke-live          Test live gateway/provider protocols (uses provider tokens)"
	@echo "  smoke-google-dlp    Test Google DLP with ADC (uses billable API calls)"
	@echo "  opa-test            Run OPA policy tests"
	@echo "  provision           Run IaC provisioner (idempotent)"
	@echo "  onboard-help        Show user/service-account onboarding CLI help"
	@echo "  rotate-partitions   Rotate audit_log partitions (runs nightly on configured scheduler)"
	@echo "  demo                Run 6 governance scenarios (make up + provision + demo.py)"

