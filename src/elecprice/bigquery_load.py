"""Load the Parquet lake into BigQuery raw tables for the dbt `bigquery` target.

Not used until the owner creates GCP credentials (see docs/bigquery.md).

Each ingestion dataset becomes one table, ``<BQ_RAW_DATASET>.<source>_<name>``,
matching the source names in ``dbt/models/staging/_sources.yml``. The lake
stores naive UTC timestamps; they are made timezone-aware before loading so
BigQuery types them TIMESTAMP (not DATETIME), which is what the models compare
against.

    elec load-bigquery --dry-run          # plan only, no credentials needed
    elec load-bigquery                    # full reload (WRITE_TRUNCATE)
    elec load-bigquery --recent-days 7    # append recent chunks; staging dedups
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from elecprice.config import get_settings
from elecprice.ingest import REGISTRY
from elecprice.logging_utils import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class LoadPlan:
    table: str
    files: list[Path]

    @property
    def rows(self) -> int:
        import pyarrow.parquet as pq

        return sum(pq.ParquetFile(f).metadata.num_rows for f in self.files)


def _chunk_end(path: Path) -> date:
    # Files are named <start>_<end>.parquet (YYYYMMDD).
    return date.fromisoformat(pd.Timestamp(path.stem.split("_")[1]).strftime("%Y-%m-%d"))


def plan(lake_dir: Path | None = None, recent_days: int | None = None) -> list[LoadPlan]:
    lake_dir = lake_dir or get_settings().lake_dir
    cutoff = date.today() - timedelta(days=recent_days) if recent_days else None
    plans = []
    for ds in REGISTRY.values():
        files = sorted((lake_dir / ds.source / ds.name).glob("*.parquet"))
        if cutoff:
            files = [f for f in files if _chunk_end(f) >= cutoff]
        if files:
            plans.append(LoadPlan(f"{ds.source}_{ds.name}", files))
    return plans


def to_bigquery_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Naive datetimes (UTC by convention) -> tz-aware UTC, so BigQuery types TIMESTAMP."""
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]) and out[col].dt.tz is None:
            out[col] = out[col].dt.tz_localize("UTC")
    return out


def load(recent_days: int | None = None, dry_run: bool = False) -> list[LoadPlan]:
    plans = plan(recent_days=recent_days)
    project = os.environ.get("GCP_PROJECT")
    dataset = os.environ.get("BQ_RAW_DATASET", "elecprice_raw")
    location = os.environ.get("BQ_LOCATION", "europe-west2")
    for p in plans:
        log.info("%s: %d files, %d rows", p.table, len(p.files), p.rows)
    if dry_run:
        return plans
    if not project:
        raise RuntimeError("set GCP_PROJECT (and GOOGLE_APPLICATION_CREDENTIALS)")

    from google.cloud import bigquery

    client = bigquery.Client(project=project, location=location)
    client.create_dataset(bigquery.Dataset(f"{project}.{dataset}"), exists_ok=True)
    disposition = (
        bigquery.WriteDisposition.WRITE_APPEND
        if recent_days
        else bigquery.WriteDisposition.WRITE_TRUNCATE
    )
    for p in plans:
        frame = to_bigquery_frame(pd.concat([pd.read_parquet(f) for f in p.files]))
        job = client.load_table_from_dataframe(
            frame,
            f"{project}.{dataset}.{p.table}",
            job_config=bigquery.LoadJobConfig(write_disposition=disposition),
        )
        job.result()
        log.info("loaded %s (%d rows)", p.table, len(frame))
    return plans
