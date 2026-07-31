.PHONY: up down restart status logs migrate lint test test-integration opa-test provision onboard-help rotate-partitions demo help

JWT_SECRET ?= local-dev-jwt-secret-for-compose-tests-only
GOVERNANCE_INTERNAL_TOKEN ?= local-dev-governance-token
PSEUDONYM_HMAC_KEY ?= local-dev-pseudonym-hmac-key-for-compose-tests-only
GATEWAY_PROXY_PORT ?= 18765
GATEWAY_POSTGRES_PORT ?= 15433
GATEWAY_MCPPROXY_PORT ?= 18766
GATEWAY_OPA_SIDECAR_PORT ?= 18767
GATEWAY_BASE_URL ?= http://localhost:$(GATEWAY_PROXY_PORT)
DATABASE_URL ?= postgresql://gateway:gateway@localhost:$(GATEWAY_POSTGRES_PORT)/gateway
export JWT_SECRET GOVERNANCE_INTERNAL_TOKEN PSEUDONYM_HMAC_KEY GATEWAY_PROXY_PORT GATEWAY_POSTGRES_PORT GATEWAY_MCPPROXY_PORT GATEWAY_OPA_SIDECAR_PORT GATEWAY_BASE_URL DATABASE_URL

## Service lifecycle
up:
	docker compose up -d --wait

down:
	docker compose down

restart:
	$(MAKE) down
	$(MAKE) up

status:
	docker compose ps

logs:
	docker compose logs -f

## Database
migrate:
	docker compose run --rm migrate

## Code quality
lint:
	cd proxy && uv --no-config run --extra dev ruff check . && uv --no-config run --extra dev ty check app
	cd governance && uv --no-config run --extra dev ruff check app && uv --no-config run --extra dev ty check app

test:
	cd proxy && uv --no-config run --extra dev python -m pytest tests/
	cd governance && uv --no-config run --extra dev python -m pytest tests/

test-integration:
	MOCK_PROVIDERS=true $(MAKE) up
	$(MAKE) provision
	cd proxy && INTEGRATION_TEST=1 uv --no-config run pytest ../tests/integration/ -v

## OPA policy tests
opa-test:
	docker compose run --rm opa test /policies -v

## Provisioning
provision:
	uv --no-config run --with psycopg2-binary --with pyyaml --with bcrypt scripts/provision.py

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
	@echo "  opa-test            Run OPA policy tests"
	@echo "  provision           Run IaC provisioner (idempotent)"
	@echo "  onboard-help        Show user/service-account onboarding CLI help"
	@echo "  rotate-partitions   Rotate audit_log partitions (runs nightly on configured scheduler)"
	@echo "  demo                Run 6 governance scenarios (make up + provision + demo.py)"

