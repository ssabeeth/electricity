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
    assert set(dagbag.dag_ids) == {
        "elec_bootstrap",
        "elec_ingest",
        "elec_daily_forecast",
        "elec_weekly_retrain",
    }


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


def test_daily_forecast_uses_the_runs_own_time(dagbag):
    dag = dagbag.dags["elec_daily_forecast"]
    for task_id in ("forecast_tomorrow", "battery_schedule"):
        assert "--as-of '{{ dag_run.run_after }}'" in dag.get_task(task_id).bash_command


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


def test_warehouse_tasks_share_the_single_slot_pool(dagbag):
    """Anything that opens DuckDB (dbt, forecast, monitor, retrain) must use the pool."""
    for dag in dagbag.dags.values():
        for task in dag.tasks:
            cmd = task.bash_command
            opens_warehouse = any(
                k in cmd
                for k in ("dbt ", " forecast", " monitor", " retrain", " backtest", "--step")
            )
            if opens_warehouse:
                assert task.pool == "warehouse", (dag.dag_id, task.task_id)


def test_bootstrap_runs_once_in_order(dagbag):
    dag = dagbag.dags["elec_bootstrap"]
    assert chain(dag) == [
        "ingest_history",
        "dbt_build",
        "backtest",
        "register_champion",
        "battery_simulation",
        "first_forecast",
    ]
