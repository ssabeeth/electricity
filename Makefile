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

.PHONY: backtest
backtest: ## Walk-forward backtest, log to MLflow, regenerate reports/backtest.md
	$(UV) run elec backtest

.PHONY: train
train: ## Train on all data and register as the champion model
	$(UV) run elec train --alias champion

.PHONY: mlflow-ui
mlflow-ui: ## Local MLflow UI on the sqlite store
	$(UV) run mlflow ui --backend-store-uri sqlite:///data/mlflow/mlflow.db --port 5000

.PHONY: simulate
simulate: ## Battery arbitrage simulation over the backtest forecasts + reports/battery.md
	$(UV) run elec simulate

AIRFLOW_VERSION ?= 3.3.2
.PHONY: test-airflow
test-airflow: ## DAG integrity tests in a separate Airflow venv
	test -x .venv-airflow/bin/python || ( $(UV) venv .venv-airflow --python 3.12 && \
	  $(UV) pip install --python .venv-airflow/bin/python "apache-airflow==$(AIRFLOW_VERSION)" pytest \
	  --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-$(AIRFLOW_VERSION)/constraints-3.12.txt" )
	AIRFLOW_HOME=$${TMPDIR:-/tmp}/airflow_home .venv-airflow/bin/python -m pytest -q -p no:cacheprovider tests/airflow

.PHONY: daily
daily: ## What the daily DAG does: ingest, dbt build, forecast, schedule, monitor
	$(UV) run elec ingest --days 3
	$(UV) run elec dbt build
	$(UV) run elec forecast
	$(UV) run elec schedule
	$(UV) run elec monitor

.PHONY: retrain
retrain: ## Champion/challenger retrain
	$(UV) run elec retrain

.PHONY: api
api: ## Run the FastAPI service on :8000 (docs at /docs)
	$(UV) run uvicorn elecprice.serving.api:app --reload --port 8000

.PHONY: dashboard
dashboard: ## Run the Streamlit dashboard on :8501 (in-process API, no server needed)
	ELEC_API_URL=$${ELEC_API_URL:-inprocess} $(UV) run streamlit run src/elecprice/serving/dashboard.py

.PHONY: env
env: ## Create .env with random secrets if missing
	./scripts/init_env.sh

.PHONY: up
up: env ## Build and start the full stack (Airflow, MLflow, API, dashboard)
	docker compose up -d --build
	@echo ""
	@echo "  Airflow    http://localhost:$${AIRFLOW_PORT:-8080}  (admin / see .env)"
	@echo "  MLflow     http://localhost:$${MLFLOW_PORT:-5001}"
	@echo "  API        http://localhost:$${API_PORT:-8000}/docs"
	@echo "  Dashboard  http://localhost:$${DASHBOARD_PORT:-8501}"
	@echo ""
	@echo "  First start: the elec_bootstrap DAG builds everything (about 15 min on a clean clone)."

.PHONY: down
down: ## Stop the stack (data in ./data and volumes is kept)
	docker compose down

.PHONY: ps
ps: ## Show stack status
	docker compose ps

.PHONY: logs
logs: ## Tail stack logs
	docker compose logs -f --tail=100
