"""Run dbt with the project's paths wired in via environment variables.

``elec dbt build`` is equivalent to ``dbt build`` run from ``dbt/`` with
``ELEC_LAKE_DIR`` and ``ELEC_DUCKDB_PATH`` set to absolute paths, so the same
command works from Make, Airflow, CI and containers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from elecprice.config import Settings, get_settings
from elecprice.logging_utils import get_logger

log = get_logger(__name__)


def dbt_env(settings: Settings | None = None) -> dict[str, str]:
    s = settings or get_settings()
    env = os.environ.copy()
    env.setdefault("DBT_PROFILES_DIR", str(s.dbt_project_dir))
    env.setdefault("ELEC_LAKE_DIR", str(s.lake_dir))
    env.setdefault("ELEC_DUCKDB_PATH", str(s.warehouse_path))
    return env


def _dbt_executable() -> str:
    # Prefer the dbt installed next to the running interpreter (same venv).
    local = Path(sys.executable).parent / "dbt"
    if local.exists():
        return str(local)
    found = shutil.which("dbt")
    if not found:
        raise FileNotFoundError("dbt not found; install the 'dbt' extra")
    return found


def run_dbt(args: list[str], settings: Settings | None = None) -> int:
    s = settings or get_settings()
    s.warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_dbt_executable(), *args]
    log.info("running %s (cwd=%s)", " ".join(cmd), s.dbt_project_dir)
    return subprocess.run(cmd, cwd=s.dbt_project_dir, env=dbt_env(s), check=False).returncode
