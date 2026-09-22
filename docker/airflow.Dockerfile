# Airflow image with the project installed in its own virtualenv.
# DAG tasks call $ELEC_BIN, so the project's dependencies never mix with
# Airflow's constrained environment.
FROM apache/airflow:3.3.2-python3.12

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/python/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/elec/venv

WORKDIR /opt/elec
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra ml --extra dbt
COPY src ./src
COPY configs ./configs
COPY dbt ./dbt
COPY reports ./reports
RUN uv sync --frozen --no-dev --extra ml --extra dbt \
    && mkdir -p /data \
    && chown -R airflow:0 /opt/elec /data \
    && chmod -R g+rwX /opt/elec /data

COPY --chown=airflow:0 airflow/dags /opt/airflow/dags

ENV ELEC_BIN=/opt/elec/venv/bin/elec \
    ELEC_DATA_DIR=/data \
    ELEC_CONFIG_DIR=/opt/elec/configs \
    ELEC_DBT_PROJECT_DIR=/opt/elec/dbt \
    MLFLOW_DISABLE_AGENT_HINT=1

WORKDIR /opt/airflow
USER airflow
