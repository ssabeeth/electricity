"""Chunked, cached, idempotent ingestion.

Each dataset is fetched in fixed-size date chunks. Chunk boundaries are aligned
to a fixed epoch, so the same chunk always has the same key whatever start date
a run is given. For every chunk we keep:

* the raw API response, gzipped, under ``data/raw/<source>/<dataset>/<key>.json.gz``
* a normalised Parquet file under ``data/lake/<source>/<dataset>/<key>.parquet``

A chunk is *settled* once its last day is more than ``refresh_days`` in the
past. Settled chunks are never fetched again (unless ``force``), and if the
Parquet file is missing it is rebuilt from the raw cache without touching the
network. Recent chunks are re-fetched on every run because the source may still
be filling them in. Writes are atomic (temp file + rename), so an interrupted
run never leaves a half-written file behind.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from elecprice.config import Settings, get_settings
from elecprice.logging_utils import get_logger

log = get_logger(__name__)

EPOCH = date(2020, 1, 6)  # a Monday; all chunk boundaries are aligned to it


def aligned_chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """Chunks of ``days`` days aligned to ``EPOCH`` that together cover ``[start, end]``."""
    if end < start:
        return []
    first = start - timedelta(days=(start - EPOCH).days % days)
    out = []
    cur = first
    while cur <= end:
        out.append((cur, cur + timedelta(days=days - 1)))
        cur += timedelta(days=days)
    return out


@dataclass(frozen=True)
class Dataset:
    source: str
    name: str
    chunk_days: int
    fetch: Callable[[date, date], Any]
    """Return a JSON-serialisable payload covering the inclusive chunk ``[start, end]``."""
    normalise: Callable[[Any], pd.DataFrame]
    """Turn a payload into a tidy DataFrame (raw column semantics, typed)."""
    lookahead_days: int = 0
    """Ingest this many days past today (e.g. weather forecasts for tomorrow)."""
    min_start: date | None = None
    """Earliest date the source has data for; earlier requests are skipped."""
    description: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}/{self.name}"


@dataclass
class IngestReport:
    dataset: str
    chunks: int = 0
    fetched: int = 0
    from_cache: int = 0
    skipped: int = 0
    rows_written: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def __str__(self) -> str:
        return (
            f"{self.dataset}: chunks={self.chunks} fetched={self.fetched} "
            f"from_cache={self.from_cache} skipped={self.skipped} "
            f"rows={self.rows_written} failed={len(self.failed)}"
        )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class ChunkStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @staticmethod
    def chunk_key(start: date, end: date) -> str:
        return f"{start:%Y%m%d}_{end:%Y%m%d}"

    def raw_path(self, ds: Dataset, key: str) -> Path:
        return self.settings.raw_dir / ds.source / ds.name / f"{key}.json.gz"

    def lake_path(self, ds: Dataset, key: str) -> Path:
        return self.settings.lake_dir / ds.source / ds.name / f"{key}.parquet"

    def write_raw(self, ds: Dataset, key: str, payload: Any) -> None:
        data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0)
        _atomic_write_bytes(self.raw_path(ds, key), data)

    def read_raw(self, ds: Dataset, key: str) -> Any:
        with gzip.open(self.raw_path(ds, key), "rt") as fh:
            return json.load(fh)

    def write_lake(self, ds: Dataset, key: str, df: pd.DataFrame) -> None:
        path = self.lake_path(ds, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        os.close(fd)
        try:
            df.to_parquet(tmp, index=False)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _process_chunk(
    ds: Dataset,
    store: ChunkStore,
    start: date,
    end: date,
    *,
    fetch_end: date,
    settled: bool,
    force: bool,
) -> tuple[str, int]:
    """Process one aligned chunk. Returns (outcome, rows).

    ``outcome`` is 'skipped' | 'cache' | 'fetched'. The chunk is stored under its
    aligned key ``[start, end]`` but only ``[start, fetch_end]`` is requested, so
    we never ask a source for days past the ingestion horizon.
    """
    key = store.chunk_key(start, end)
    raw_exists = store.raw_path(ds, key).exists()
    lake_exists = store.lake_path(ds, key).exists()

    if settled and not force and raw_exists and lake_exists:
        return "skipped", 0

    if settled and not force and raw_exists:
        payload = store.read_raw(ds, key)
        outcome = "cache"
    else:
        payload = ds.fetch(start, fetch_end)
        store.write_raw(ds, key, payload)
        outcome = "fetched"

    df = ds.normalise(payload)
    if df.empty:
        log.info("%s %s: no rows", ds.key, key)
        store.lake_path(ds, key).unlink(missing_ok=True)
        return outcome, 0
    df = df.assign(
        _chunk=key,
        _ingested_at=pd.Timestamp(datetime.now(UTC)).floor("s").tz_localize(None),
    )
    store.write_lake(ds, key, df)
    return outcome, len(df)


def ingest_dataset(
    ds: Dataset,
    start: date,
    end: date | None = None,
    *,
    force: bool = False,
    refresh_days: int = 3,
    today: date | None = None,
    workers: int = 1,
    store: ChunkStore | None = None,
) -> IngestReport:
    """Ingest ``ds`` for dates ``[start, end]`` (delivery or publish dates per dataset).

    ``end`` defaults to, and is capped at, today plus the dataset's look-ahead.
    """
    store = store or ChunkStore()
    today = today or datetime.now(UTC).date()
    horizon = today + timedelta(days=ds.lookahead_days)
    end = min(end or horizon, horizon)
    if ds.min_start:
        start = max(start, ds.min_start)
    report = IngestReport(dataset=ds.key)

    chunks = aligned_chunks(start, end, ds.chunk_days)
    report.chunks = len(chunks)
    settle_line = today - timedelta(days=refresh_days)

    def run(chunk: tuple[date, date]) -> tuple[str, int]:
        c_start, c_end = chunk
        return _process_chunk(
            ds,
            store,
            c_start,
            c_end,
            fetch_end=min(c_end, horizon),
            settled=c_end < settle_line,
            force=force,
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(run, c): c for c in chunks}
        for fut in as_completed(futures):
            c_start, c_end = futures[fut]
            try:
                outcome, rows = fut.result()
            except Exception as exc:  # keep going; report at the end
                key = store.chunk_key(c_start, c_end)
                log.error("%s %s failed: %s", ds.key, key, exc)
                report.failed.append(key)
                continue
            report.rows_written += rows
            if outcome == "skipped":
                report.skipped += 1
            elif outcome == "cache":
                report.from_cache += 1
            else:
                report.fetched += 1

    log.info("%s", report)
    return report
