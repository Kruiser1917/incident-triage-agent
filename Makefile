.DEFAULT_GOAL := help
.PHONY: help install up up-obs down test test-integration lint format evals

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

install: ## Install dependencies into .venv
	uv sync

up: ## Start core services (Postgres + pgvector) and wait until healthy
	docker compose up -d --wait

up-obs: ## Start core services plus the Langfuse stack
	docker compose --profile obs up -d --wait

down: ## Stop all services (data volumes are kept)
	docker compose --profile obs down

test: ## Run unit tests
	uv run pytest

test-integration: ## Run integration tests (requires `make up`)
	uv run pytest -m integration

lint: ## Ruff lint + format check + mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src tests

format: ## Auto-fix lint issues and format code
	uv run ruff check --fix .
	uv run ruff format .

evals: ## Run evals (implemented in phase 7)
	@echo "evals: not implemented yet (phase 7)" >&2; exit 1
