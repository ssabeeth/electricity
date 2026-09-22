from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURE_LAKE = REPO / "tests" / "fixtures" / "lake"
# The fixture lake covers 2024-03-01..2024-04-07; start features a week in so
# price lags have history.
FIXTURE_VARS = {"feature_start_date": "2024-03-08", "feature_end_date": "2024-04-07"}


def run_fixture_dbt(args: list[str], warehouse: Path, workdir: Path, extra_vars=None):
    env = os.environ.copy()
    env.update(
        {
            "DBT_PROFILES_DIR": str(REPO / "dbt"),
            "DBT_TARGET": "duckdb",
            "ELEC_LAKE_DIR": str(FIXTURE_LAKE),
            "ELEC_DUCKDB_PATH": str(warehouse),
            "DBT_TARGET_PATH": str(workdir / "target"),
            "DBT_LOG_PATH": str(workdir / "logs"),
        }
    )
    dbt = str(Path(sys.executable).parent / "dbt")
    vars_ = {**FIXTURE_VARS, **(extra_vars or {})}
    return subprocess.run(
        [dbt, *args, "--vars", json.dumps(vars_)],
        cwd=REPO / "dbt",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture(scope="session")
def fixture_warehouse(tmp_path_factory) -> Path:
    """A DuckDB warehouse built by `dbt build` over the committed fixture lake."""
    pytest.importorskip("dbt")
    workdir = tmp_path_factory.mktemp("dbt")
    warehouse = workdir / "fixture.duckdb"
    result = run_fixture_dbt(["build"], warehouse, workdir)
    if result.returncode != 0:
        pytest.fail(f"dbt build on fixtures failed:\n{result.stdout[-4000:]}")
    return warehouse
