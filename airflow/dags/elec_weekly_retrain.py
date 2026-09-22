"""Weekly retrain with a champion/challenger gate (Sunday 06:00 UK).

The retrain step judges last week's challenger against the champion on days
neither model has seen, promotes it only if its pinball loss is lower, then
registers a new challenger trained on all data. The backtest and battery
simulation are refreshed afterwards so the dashboard's metrics stay current.
"""

from __future__ import annotations

from datetime import timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from elec_common import DEFAULT_ARGS, START, WAREHOUSE_POOL, elec

with DAG(
    dag_id="elec_weekly_retrain",
    description="Champion/challenger retrain, backtest refresh and battery simulation",
    schedule="0 6 * * 0",
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    dagrun_timeout=timedelta(hours=2),
    tags=["elecprice", "training"],
    doc_md=__doc__,
) as dag:
    ingest = BashOperator(task_id="ingest_latest", bash_command=elec("ingest --days 7"))
    dbt_build = BashOperator(
        task_id="dbt_build", bash_command=elec("dbt build"), pool=WAREHOUSE_POOL
    )
    retrain = BashOperator(
        task_id="retrain_and_promote", bash_command=elec("retrain"), pool=WAREHOUSE_POOL
    )
    backtest = BashOperator(
        task_id="refresh_backtest", bash_command=elec("backtest"), pool=WAREHOUSE_POOL
    )
    simulate = BashOperator(task_id="refresh_battery_simulation", bash_command=elec("simulate"))

    ingest >> dbt_build >> retrain >> backtest >> simulate
