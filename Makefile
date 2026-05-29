.PHONY: up down restart status logs migrate lint test test-integration opa-test provision rotate-partitions demo deploy help

## Service lifecycle
up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose down && docker compose up -d

status:
	docker compose ps

logs:
	docker compose logs -f

## Database
migrate:
	docker compose run --rm migrate

## Code quality
lint:
	cd proxy && uv run ruff check . && uv run pyright
	cd governance && uv run ruff check . && uv run pyright

test:
	cd proxy && uv run pytest tests/
	cd governance && uv run pytest tests/

test-integration:
	cd proxy && INTEGRATION_TEST=1 GATEWAY_BASE_URL=http://localhost:8765 uv run pytest ../tests/integration/ -v

## OPA policy tests
opa-test:
	docker compose run --rm opa test /policies -v

## Provisioning
provision:
	uv run scripts/provision.py

rotate-partitions:
	cd governance && uv run python ../scripts/rotate_partitions.py

## Demo
demo:
	$(MAKE) up
	$(MAKE) provision
	MOCK_PROVIDERS=true uv run scripts/demo.py

## Fly.io deployment (OPA → governance → proxy order)
deploy:
	fly deploy --config fly-opa.toml
	fly deploy --config fly-governance.toml
	fly deploy --config fly.toml

## Help
help:
	@echo "Available targets:"
	@echo "  up                  Start all services (detached)"
	@echo "  down                Stop all services"
	@echo "  restart             Restart all services"
	@echo "  status              Show service status"
	@echo "  logs                Follow service logs"
	@echo "  migrate             Run database migrations"
	@echo "  lint                Run ruff + pyright on both services"
	@echo "  test                Run pytest on both services"
	@echo "  test-integration    Run Docker Compose smoke tests (requires make up)"
	@echo "  opa-test            Run OPA policy tests"
	@echo "  provision           Run IaC provisioner (idempotent)"
	@echo "  rotate-partitions   Rotate audit_log partitions (runs nightly on Fly cron)"
	@echo "  demo                Run 6 governance scenarios (make up + provision + demo.py)"
	@echo "  deploy              Deploy all services to Fly.io (OPA → governance → proxy)"
