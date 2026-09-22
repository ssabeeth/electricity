"""Shared settings for the elecprice DAGs.

Every task shells out to the ``elec`` CLI, installed in its own virtualenv
inside the Airflow image (``ELEC_BIN``). That keeps the project's dependencies
(LightGBM, dbt, MLflow...) out of Airflow's constrained environment, and it
means Airflow runs exactly the commands a developer runs locally.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pendulum

ELEC_BIN = os.environ.get("ELEC_BIN", "elec")
# DuckDB allows one writer process. Every task that opens the warehouse runs in
# this single-slot pool, so DAGs never contend for the file lock.
WAREHOUSE_POOL = "warehouse"
UK = pendulum.timezone("Europe/London")
START = pendulum.datetime(2026, 9, 1, tz=UK)

DEFAULT_ARGS = {
    "owner": "elecprice",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
}


def elec(args: str) -> str:
    return f"{ELEC_BIN} {args}"
