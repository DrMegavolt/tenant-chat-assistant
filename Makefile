SHELL := /bin/bash
.DEFAULT_GOAL := help

# Every recipe runs through `uv run --frozen` so a stale or hand-edited
# environment cannot silently change what is being verified. `--frozen` fails if
# uv.lock does not match pyproject.toml rather than resolving something new.
UV_RUN := uv run --frozen

# The frontend is a self-contained npm project; nothing at the repository root
# is an npm package.
NPM := npm --prefix frontend

.PHONY: help setup lock lock-check lint format format-check typecheck test test-cov \
	test-migrations test-repositories test-database migrate dev js-install js-lint js-format \
	js-format-check js-test js-test-cov deployment-security check api up up-all web down \
	down-clean logs ps network-policy-smoke image-contracts images-build images-smoke \
	images-check arch-validate arch-build clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv from the lockfile and seed .env
	uv sync --frozen
	$(NPM) ci
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

test: ## Run the hermetic unit suite (no external services)
	$(UV_RUN) pytest -m "not integration"

test-cov: ## Run tests with a coverage report
	$(UV_RUN) pytest -m "not integration" --cov --cov-report=term-missing \
		--cov-report=xml:coverage/python/coverage.xml \
		--cov-report=html:coverage/python/html \
		--junitxml=artifacts/test-results/python.xml

test-migrations: ## Run migrations against an isolated Postgres 16 container
	$(UV_RUN) pytest -m integration tests/migrations

test-repositories: ## Run authoritative repository tests on isolated Postgres 16
	$(UV_RUN) pytest -m integration tests/repositories

test-database: test-migrations test-repositories ## Run all isolated Postgres suites

migrate: ## Upgrade with the schema-owner URL (never the application URL)
	@test -n "$${DATABASE_MIGRATION_URL}" || { echo "DATABASE_MIGRATION_URL is required"; exit 2; }
	$(UV_RUN) alembic upgrade head

frontend/node_modules/.package-lock.json: frontend/package.json frontend/package-lock.json
	$(NPM) ci

js-install: frontend/node_modules/.package-lock.json ## Install exact frontend development dependencies

dev: frontend/node_modules/.package-lock.json ## Serve the frontend with hot reload against a local backend
	$(NPM) run dev

js-lint: frontend/node_modules/.package-lock.json ## Lint frontend JavaScript
	$(NPM) run lint

js-format: frontend/node_modules/.package-lock.json ## Apply frontend JavaScript formatting
	$(NPM) run format

js-format-check: frontend/node_modules/.package-lock.json ## Fail if frontend JavaScript is unformatted
	$(NPM) run format:check

js-test: frontend/node_modules/.package-lock.json ## Run frontend JavaScript tests
	$(NPM) test

js-test-cov: frontend/node_modules/.package-lock.json ## Run frontend tests with coverage reports
	$(NPM) run test:coverage

deployment-security: ## Scan rendered non-Secret Kubernetes manifests and runtime refs
	$(UV_RUN) python scripts/verify_deployment_security.py

network-policy-smoke: ## Prove allowed and denied flows in disposable MicroK8s namespaces
	./k8s/tests/network-policy-smoke.sh

image-contracts: ## Verify immutable image and Kubernetes artifact contracts
	$(UV_RUN) python scripts/verify_image_contracts.py

images-build: ## Build all five deployable images and record local metadata/digests
	./scripts/build_images.sh

images-smoke: ## Smoke all previously built deployable images as their runtime user
	./scripts/smoke_images.sh

images-check: image-contracts images-build images-smoke ## Build and smoke all release images

check: lock-check lint format-check typecheck test-cov js-lint js-format-check js-test-cov \
	deployment-security image-contracts ## Full local and CI quality gate
	@echo ""
	@echo "quality gate passed"

api: ## Run services/api under uvicorn (the production server command)
	$(UV_RUN) uvicorn --factory tenantchat.api.app:create_app \
		--host $${CHAT_API_HOST:-127.0.0.1} --port $${CHAT_API_PORT:-8080}

up: ## Start local dependencies (Postgres, Elasticsearch)
	docker compose up -d --wait

up-all: ## Start dependencies plus the optional embedding service
	docker compose --profile embedding up -d --wait

web: ## Serve the frontend from the deployed nginx image on http://127.0.0.1:8080
	docker compose --profile web up -d --build --wait web

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
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml coverage htmlcov artifacts
	find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
