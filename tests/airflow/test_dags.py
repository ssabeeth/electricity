"""DAG integrity tests. Need Airflow, which lives in its own environment:

make test-airflow   (or the `airflow` CI job)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# The repo's own airflow/ folder imports as a namespace package, so probe a real module.
pytest.importorskip("airflow.sdk")

DAG_DIR = Path(__file__).resolve().parents[2] / "airflow" / "dags"


@pytest.fixture(scope="module")
def dagbag(tmp_path_factory):
    os.environ.setdefault("AIRFLOW_HOME", str(tmp_path_factory.mktemp("airflow_home")))
    os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
    os.environ["AIRFLOW__CORE__DAGS_FOLDER"] = str(DAG_DIR)
    if str(DAG_DIR) not in sys.path:
        sys.path.insert(0, str(DAG_DIR))  # Airflow puts the DAGs folder on sys.path
    try:
        from airflow.dag_processing.dagbag import DagBag
    except ImportError:  # Airflow < 3.1
        from airflow.models.dagbag import DagBag
    return DagBag(dag_folder=str(DAG_DIR))


def test_no_import_errors(dagbag):
    assert dagbag.import_errors == {}


def test_expected_dags(dagbag):
    assert set(dagbag.dag_ids) == {"elec_ingest", "elec_daily_forecast", "elec_weekly_retrain"}


def chain(dag):
    """Task ids in dependency order for a linear DAG."""
    order, task = [], next(t for t in dag.tasks if not t.upstream_task_ids)
    while True:
        order.append(task.task_id)
        if not task.downstream_task_ids:
            return order
        (nxt,) = task.downstream_task_ids
        task = dag.get_task(nxt)


def test_daily_forecast_runs_after_cutoff_in_uk_time(dagbag):
    dag = dagbag.dags["elec_daily_forecast"]
    assert chain(dag) == [
        "ingest_latest",
        "dbt_build",
        "forecast_tomorrow",
        "battery_schedule",
        "settle_and_monitor",
    ]
    # Cron is evaluated in UK local time, so it tracks 09:00 across clock changes.
    assert "Europe/London" in str(dag.timetable.timezone)
    assert dag.timetable.expression == "5 9 * * *"
    assert dag.catchup is False


def test_weekly_retrain_order(dagbag):
    dag = dagbag.dags["elec_weekly_retrain"]
    assert chain(dag) == [
        "ingest_latest",
        "dbt_build",
        "retrain_and_promote",
        "refresh_backtest",
        "refresh_battery_simulation",
    ]


def test_every_task_calls_the_elec_cli(dagbag):
    for dag in dagbag.dags.values():
        for task in dag.tasks:
            assert "elec " in task.bash_command, (dag.dag_id, task.task_id)
