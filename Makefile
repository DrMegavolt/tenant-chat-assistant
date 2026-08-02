SHELL := /bin/bash
.DEFAULT_GOAL := help

# Every recipe runs through `uv run --frozen` so a stale or hand-edited
# environment cannot silently change what is being verified. `--frozen` fails if
# uv.lock does not match pyproject.toml rather than resolving something new.
UV_RUN := uv run --frozen

.PHONY: help setup lock lock-check lint format format-check typecheck test test-cov check \
	api up up-all down down-clean logs ps arch-validate arch-build clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv from the lockfile and seed .env
	uv sync --frozen
	@test -f .env || { cp .env.example .env; echo "created .env from .env.example"; }

lock: ## Re-resolve dependencies and update uv.lock
	uv lock

lock-check: ## Fail if uv.lock is out of date with pyproject.toml
	uv lock --check

lint: ## Run ruff lint rules
	$(UV_RUN) ruff check .

format: ## Apply ruff formatting
	$(UV_RUN) ruff format .

format-check: ## Fail if any file is unformatted
	$(UV_RUN) ruff format --check .

typecheck: ## Run mypy in strict mode
	$(UV_RUN) mypy

test: ## Run the test suite
	$(UV_RUN) pytest

test-cov: ## Run tests with a coverage report
	$(UV_RUN) pytest --cov --cov-report=term-missing --cov-report=xml

check: lock-check lint format-check typecheck test ## Full local quality gate (what CI runs)
	@echo ""
	@echo "quality gate passed"

api: ## Run services/api under uvicorn (the production server command)
	$(UV_RUN) uvicorn --factory tenantchat.api.app:create_app \
		--host $${CHAT_API_HOST:-127.0.0.1} --port $${CHAT_API_PORT:-8080}

up: ## Start local dependencies (Postgres, Elasticsearch)
	docker compose up -d --wait

up-all: ## Start dependencies plus the optional embedding service
	docker compose --profile embedding up -d --wait

down: ## Stop local dependencies, preserving volumes
	docker compose down

down-clean: ## Stop local dependencies and delete their volumes
	docker compose down --volumes

logs: ## Tail dependency logs
	docker compose logs -f

ps: ## Show dependency status
	docker compose ps

arch-validate: ## Validate the LikeC4 architecture model
	npm --prefix architecture/likec4 run validate

arch-build: ## Regenerate architecture diagrams
	npm --prefix architecture/likec4 run export:png

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
