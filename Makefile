# AgentTape developer commands. `make help` lists them.
SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help
BACKEND := cd backend &&

.PHONY: help setup dev check build migrate seed reset-demo test lint typecheck format e2e openapi

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup: ## Install backend (uv) and frontend/e2e (pnpm) dependencies and initialise the data dir
	$(BACKEND) uv sync --frozen
	pnpm install --frozen-lockfile
	$(BACKEND) uv run agenttape init

dev: ## Run API + worker (:8787) and the Vite dev server (:5173)
	./scripts/dev.sh

check: ## Full quality gate (lint, types, tests, build, migrations, secret scan, e2e)
	./scripts/check.sh

build: ## Production build of the web UI into the Python package
	./scripts/build-frontend.sh

migrate: ## Apply database migrations
	$(BACKEND) uv run agenttape migrate

seed: ## Create demo data (idempotent, demo-tagged)
	$(BACKEND) uv run agenttape seed

reset-demo: ## Delete demo-tagged data only
	$(BACKEND) uv run agenttape reset-demo --yes

test: ## Backend and frontend unit/integration tests
	$(BACKEND) uv run pytest -q
	pnpm --dir frontend exec vitest run

lint: ## Linters and format checks
	$(BACKEND) uv run ruff format --check . && uv run ruff check .
	pnpm --dir frontend exec eslint . && pnpm --dir frontend exec prettier --check .

typecheck: ## Type checkers
	$(BACKEND) uv run mypy
	pnpm --dir frontend exec tsc --noEmit -p .

format: ## Apply formatters
	$(BACKEND) uv run ruff format . && uv run ruff check --fix .
	pnpm --dir frontend exec prettier --write .

e2e: build ## Playwright end-to-end tests against an isolated server
	cd e2e && pnpm exec playwright install chromium && pnpm exec playwright test

openapi: ## Print the OpenAPI document
	$(BACKEND) uv run agenttape openapi
