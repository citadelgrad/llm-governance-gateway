.PHONY: up down restart status logs migrate lint test opa-test provision rotate-bootstrap rotate-partitions demo help

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
	INTEGRATION_TEST=1 uv run pytest tests/integration/ -v

## OPA policy tests
opa-test:
	docker compose run --rm opa test /policies -v

## Provisioning
provision:
	uv run scripts/provision.py

rotate-bootstrap:
	docker compose exec governance uv run scripts/rotate_bootstrap.py

rotate-partitions:
	docker compose exec governance uv run scripts/rotate_partitions.py

## Demo
demo:
	@echo "Starting demo environment..."
	$(MAKE) up
	@echo "Waiting for services..."
	@sleep 10
	$(MAKE) provision
	@echo "Demo ready. Run 'make logs' to follow output."

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
	@echo "  opa-test            Run OPA policy tests"
	@echo "  provision           Run IaC provisioner (idempotent)"
	@echo "  rotate-bootstrap    Rotate bootstrap token"
	@echo "  rotate-partitions   Rotate partition keys"
	@echo "  demo                Start full demo environment"
