"""Daily day-ahead forecast, just after the 09:00 UK decision cutoff.

ingest latest vintages -> dbt build (includes the point-in-time tests) ->
forecast D+1 with the champion model -> battery schedule -> settle past days.

If the point-in-time test fails, dbt build fails and no forecast is produced.

The delivery day comes from the run's own time (``run_after``), not the wall
clock, so retries, late runs and manual re-runs forecast the right day.
"""

from __future__ import annotations

from datetime import timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from elec_common import DEFAULT_ARGS, START, WAREHOUSE_POOL, elec

with DAG(
    dag_id="elec_daily_forecast",
    description="Forecast tomorrow's half-hourly prices and schedule the battery",
    schedule="5 9 * * *",  # Europe/London (from start_date)
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    dagrun_timeout=timedelta(hours=1),
    tags=["elecprice", "forecast"],
    doc_md=__doc__,
) as dag:
    ingest = BashOperator(
        task_id="ingest_latest",
        bash_command=elec("ingest --days 3"),
        retries=3,
        retry_delay=timedelta(minutes=5),
    )
    dbt_build = BashOperator(
        task_id="dbt_build", bash_command=elec("dbt build"), pool=WAREHOUSE_POOL
    )
    as_of = "--as-of '{{ dag_run.run_after }}'"
    forecast = BashOperator(
        task_id="forecast_tomorrow", bash_command=elec(f"forecast {as_of}"), pool=WAREHOUSE_POOL
    )
    schedule = BashOperator(task_id="battery_schedule", bash_command=elec(f"schedule {as_of}"))
    monitor = BashOperator(
        task_id="settle_and_monitor", bash_command=elec("monitor"), pool=WAREHOUSE_POOL
    )

    ingest >> dbt_build >> forecast >> schedule >> monitor
