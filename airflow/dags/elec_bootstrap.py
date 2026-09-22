"""One-off bootstrap after `make up` on a clean clone (runs once, automatically).

Ingests the full history (cached, so a re-run is cheap), builds the dbt
project, runs the walk-forward backtest, registers a champion model if none
exists, runs the battery simulation, and issues the first live forecast. Every
step is idempotent. Re-trigger it from the UI at any time.
"""

from __future__ import annotations

from datetime import timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from elec_common import DEFAULT_ARGS, START, WAREHOUSE_POOL, elec

with DAG(
    dag_id="elec_bootstrap",
    description="First-run setup: history, features, backtest, champion, simulation",
    schedule="@once",
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args={**DEFAULT_ARGS, "retries": 2},
    dagrun_timeout=timedelta(hours=3),
    tags=["elecprice", "setup"],
    doc_md=__doc__,
) as dag:
    ingest = BashOperator(
        task_id="ingest_history", bash_command=elec("ingest"), execution_timeout=timedelta(hours=1)
    )
    dbt_build = BashOperator(
        task_id="dbt_build", bash_command=elec("dbt build"), pool=WAREHOUSE_POOL
    )
    backtest = BashOperator(
        task_id="backtest",
        bash_command=f"test -f $ELEC_DATA_DIR/outputs/backtest/summary.csv || {elec('backtest')}",
        pool=WAREHOUSE_POOL,
    )
    champion = BashOperator(
        task_id="register_champion",
        bash_command=elec("bootstrap --step champion"),
        pool=WAREHOUSE_POOL,
    )
    simulate = BashOperator(
        task_id="battery_simulation",
        bash_command=f"test -f $ELEC_DATA_DIR/outputs/battery/summary.csv || {elec('simulate')}",
    )
    forecast = BashOperator(
        task_id="first_forecast",
        bash_command=elec("bootstrap --step forecast"),
        pool=WAREHOUSE_POOL,
    )
    ingest >> dbt_build >> backtest >> champion >> simulate >> forecast
