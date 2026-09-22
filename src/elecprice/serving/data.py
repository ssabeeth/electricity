"""Read-only access to the pipeline's Parquet/CSV outputs, cached by file mtime.

The API never touches the DuckDB warehouse (single-writer); it only reads the
small output files that the pipeline writes atomically.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pandas as pd

from elecprice.config import get_settings


class OutputStore:
    def __init__(self, root: Path | None = None):
        self.root = root or get_settings().outputs_dir
        self._cache: dict[Path, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def _load(self, rel: str, reader) -> object | None:
        path = self.root / rel
        if not path.exists():
            return None
        mtime = path.stat().st_mtime
        with self._lock:
            hit = self._cache.get(path)
            if hit and hit[0] == mtime:
                return hit[1]
        value = reader(path)
        with self._lock:
            self._cache[path] = (mtime, value)
        return value

    def frame(self, rel: str) -> pd.DataFrame:
        if rel.endswith(".csv"):
            df = self._load(rel, pd.read_csv)
        else:
            df = self._load(rel, pd.read_parquet)
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

    def json(self, rel: str) -> dict:
        value = self._load(rel, lambda p: json.loads(p.read_text()))
        return value if isinstance(value, dict) else {}

    # Named outputs ---------------------------------------------------------
    def live_forecasts(self) -> pd.DataFrame:
        return self.frame("live/forecasts.parquet")

    def live_schedules(self) -> pd.DataFrame:
        return self.frame("live/battery_schedules.parquet")

    def live_forecast_metrics(self) -> pd.DataFrame:
        return self.frame("live/forecast_daily_metrics.parquet")

    def live_battery_daily(self) -> pd.DataFrame:
        return self.frame("live/battery_daily.parquet")

    def backtest_predictions(self) -> pd.DataFrame:
        return self.frame("backtest/predictions.parquet")

    def backtest_summary(self) -> pd.DataFrame:
        return self.frame("backtest/summary.csv")

    def backtest_folds(self) -> pd.DataFrame:
        return self.frame("backtest/fold_metrics.csv")

    def backtest_importance(self) -> pd.DataFrame:
        return self.frame("backtest/importance.csv")

    def backtest_meta(self) -> dict:
        return self.json("backtest/meta.json")

    def battery_daily(self) -> pd.DataFrame:
        return self.frame("battery/daily.parquet")

    def battery_summary(self) -> pd.DataFrame:
        return self.frame("battery/summary.csv")

    def battery_schedules(self) -> pd.DataFrame:
        return self.frame("battery/schedules.parquet")

    def battery_params(self) -> dict:
        return self.json("battery/params.json")
