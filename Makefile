.PHONY: up down restart status logs migrate lint test test-integration opa-test provision rotate-partitions demo help

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
	cd proxy && uv run --extra dev ruff check . && uv run --extra dev ty check app
	cd governance && uv run --extra dev ruff check app && uv run --extra dev ty check app

test:
	cd proxy && uv run pytest tests/
	cd governance && uv run pytest tests/

test-integration:
	MOCK_PROVIDERS=true $(MAKE) up
	$(MAKE) provision
	cd proxy && INTEGRATION_TEST=1 GATEWAY_BASE_URL=http://localhost:8765 uv run pytest ../tests/integration/ -v

## OPA policy tests
opa-test:
	docker compose run --rm opa test /policies -v

## Provisioning
provision:
	DATABASE_URL=postgresql://gateway:$${POSTGRES_PASSWORD:-gateway}@localhost:15432/gateway uv run --with psycopg2-binary --with pyyaml --with bcrypt scripts/provision.py

rotate-partitions:
	cd governance && DATABASE_URL=postgresql://gateway:$${POSTGRES_PASSWORD:-gateway}@localhost:15432/gateway uv run python ../scripts/rotate_partitions.py

## Demo
demo:
	MOCK_PROVIDERS=true $(MAKE) up
	$(MAKE) provision
	MOCK_PROVIDERS=true uv run --with httpx --with 'python-jose[cryptography]' scripts/demo.py

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
	@echo "  rotate-partitions   Rotate audit_log partitions (runs nightly on Fly cron)"
	@echo "  demo                Run 6 governance scenarios (make up + provision + demo.py)"

