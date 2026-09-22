.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

.PHONY: install
install: ## Create the virtualenv with every extra and install pre-commit hooks
	$(UV) sync --all-extras
	$(UV) run pre-commit install

.PHONY: lint
lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: fmt
fmt: ## Auto-fix lint and format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

.PHONY: test
test: ## Unit tests (no network)
	$(UV) run pytest -m "not network"

.PHONY: test-network
test-network: ## Smoke tests against the live APIs
	$(UV) run pytest -m network

.PHONY: ingest
ingest: ## Ingest all sources from ELEC_HISTORY_START to today (cached, idempotent)
	$(UV) run elec ingest

.PHONY: ingest-slice
ingest-slice: ## Ingest a 3-week development slice
	$(UV) run elec ingest --start 2024-03-18 --end 2024-04-07

.PHONY: dbt-build
dbt-build: ## Build the dbt project (seeds, snapshots, models, tests) on the local lake
	$(UV) run elec dbt build

.PHONY: dbt-freshness
dbt-freshness: ## Check source freshness
	$(UV) run elec dbt source freshness

.PHONY: dbt-docs
dbt-docs: ## Generate and serve dbt docs
	$(UV) run elec dbt docs generate
	$(UV) run elec dbt docs serve --port 8081

.PHONY: fixtures
fixtures: ## Regenerate the committed sample lake under tests/fixtures/lake
	$(UV) run python scripts/make_fixtures.py
