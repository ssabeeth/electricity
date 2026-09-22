# Application image: API, dashboard, MLflow server and one-off CLI jobs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# libgomp1: OpenMP runtime for LightGBM. curl: container health checks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv

WORKDIR /app
# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra ml --extra dbt --extra serve

COPY src ./src
COPY configs ./configs
COPY dbt ./dbt
COPY reports ./reports
RUN uv sync --frozen --no-dev --extra ml --extra dbt --extra serve \
    && useradd --create-home --uid 1000 elec \
    && mkdir -p /data \
    && chown -R elec:0 /app/dbt /app/reports /data \
    && chmod -R g+rwX /app/dbt /app/reports /data

ENV PATH=/opt/venv/bin:$PATH \
    ELEC_DATA_DIR=/data \
    ELEC_CONFIG_DIR=/app/configs \
    ELEC_DBT_PROJECT_DIR=/app/dbt \
    MLFLOW_DISABLE_AGENT_HINT=1

USER elec
EXPOSE 8000 8501 5000
CMD ["elec", "--help"]
