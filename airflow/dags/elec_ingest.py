"""Rolling ingestion of every source, every three hours.

Re-fetches only recent chunks; settled chunks are served from the raw cache,
so a run takes seconds and never re-downloads history.
"""

from __future__ import annotations

from datetime import timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from elec_common import DEFAULT_ARGS, START, elec

with DAG(
    dag_id="elec_ingest",
    description="Ingest Elexon, NESO, Open-Meteo and Carbon Intensity (last 7 days)",
    schedule="15 */3 * * *",
    start_date=START,
    catchup=False,
    max_active_runs=1,
    default_args={**DEFAULT_ARGS, "retries": 3, "retry_delay": timedelta(minutes=10)},
    tags=["elecprice", "ingestion"],
    doc_md=__doc__,
) as dag:
    ingest = BashOperator(task_id="ingest_recent", bash_command=elec("ingest --days 7"))
    freshness = BashOperator(
        task_id="source_freshness",
        bash_command=elec("dbt source freshness"),
        doc_md="Fails (and alerts) if any source has stopped updating.",
    )
    ingest >> freshness
