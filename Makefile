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
