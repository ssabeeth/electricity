"""Runtime settings, read from environment variables with sensible local defaults.

Everything path-like resolves relative to the repository root unless an absolute
path is given, so the same code works on a laptop, in CI and inside containers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

UK_TZ = "Europe/London"


def _path(env: str, default: Path) -> Path:
    raw = os.environ.get(env)
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else REPO_ROOT / p


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: _path("ELEC_DATA_DIR", REPO_ROOT / "data"))
    config_dir: Path = field(
        default_factory=lambda: _path("ELEC_CONFIG_DIR", REPO_ROOT / "configs")
    )
    dbt_project_dir: Path = field(
        default_factory=lambda: _path("ELEC_DBT_PROJECT_DIR", REPO_ROOT / "dbt")
    )
    history_start: date = field(
        default_factory=lambda: date.fromisoformat(
            os.environ.get("ELEC_HISTORY_START", "2023-09-01")
        )
    )
    # Decision cutoff: forecasts for delivery day D are frozen at this UK local
    # time on D-1. See DECISIONS.md (2026-09-22, "Decision cutoff").
    cutoff_local: time = field(
        default_factory=lambda: time.fromisoformat(os.environ.get("ELEC_CUTOFF_LOCAL", "09:00"))
    )
    mlflow_tracking_uri: str = field(
        default_factory=lambda: os.environ.get("MLFLOW_TRACKING_URI", "")
    )
    http_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("ELEC_HTTP_TIMEOUT_S", "60"))
    )

    @property
    def raw_dir(self) -> Path:
        """Cached raw API responses (gzipped JSON/CSV), one file per request chunk."""
        return self.data_dir / "raw"

    @property
    def lake_dir(self) -> Path:
        """Normalised Parquet, one file per request chunk. dbt reads from here."""
        return self.data_dir / "lake"

    @property
    def warehouse_path(self) -> Path:
        return _path("ELEC_DUCKDB_PATH", self.data_dir / "warehouse.duckdb")

    @property
    def outputs_dir(self) -> Path:
        """Forecasts, backtest predictions and simulation results served by the API."""
        return self.data_dir / "outputs"

    @property
    def tracking_uri(self) -> str:
        if self.mlflow_tracking_uri:
            return self.mlflow_tracking_uri
        return f"sqlite:///{self.data_dir / 'mlflow' / 'mlflow.db'}"


def get_settings() -> Settings:
    return Settings()
