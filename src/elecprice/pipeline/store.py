"""Tiny upsert helpers for the Parquet outputs the API serves.

Outputs are small (one row per half-hour per model), so read-modify-write of a
single Parquet file is simple and atomic enough. It also keeps the serving layer
decoupled from the DuckDB warehouse, which allows only one writer process.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd


def upsert(path: Path, new: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Replace rows of ``path`` matching ``keys`` in ``new``; append the rest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = pd.read_parquet(path)
        merged = pd.concat([old, new], ignore_index=True)
        merged = merged.drop_duplicates(keys, keep="last")
    else:
        merged = new.copy()
    merged = merged.sort_values(keys).reset_index(drop=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        merged.to_parquet(tmp, index=False)
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; other containers must read it
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return merged


def read(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()
