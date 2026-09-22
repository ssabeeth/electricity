"""Forecast models sharing one interface: ``fit(df)``, ``predict(df) -> p10/p50/p90``.

* ``SeasonalNaive`` is the baseline every result is reported against: P50 is
  the price at the same UK clock time one week earlier. Its interval comes from
  empirical residual quantiles per local hour, so it can be scored with the same
  probabilistic metrics.
* ``QuantileLGBM`` fits one LightGBM model per quantile with the pinball
  objective, then optionally applies conformal calibration: models are fitted
  on all but the last ``calibration_days`` of the training window, the
  per-quantile shift that makes each quantile hit its nominal level on that
  held-out window is recorded, and the models are refitted on the full window
  with the shifts applied at prediction time. Uncalibrated quantile GBMs are
  overconfident out of sample (P10-P90 covered ~55% instead of 80% here).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import lightgbm as lgb
import numpy as np
import pandas as pd

from elecprice.modelling.data import FEATURES, TARGET
from elecprice.modelling.metrics import qcol


def seasonal_naive_point(df: pd.DataFrame) -> pd.Series:
    """Same half-hour last week; falls back to two days ago, then the weekly mean."""
    return df["price_d7_same_period"].fillna(df["price_d2_same_period"]).fillna(df["price_7d_mean"])


def _sort_quantiles(pred: np.ndarray) -> np.ndarray:
    """Independent quantile models can cross; sorting restores monotonicity."""
    return np.sort(pred, axis=1)


class SeasonalNaive:
    name: ClassVar[str] = "seasonal_naive"

    def __init__(self, quantiles=(0.1, 0.5, 0.9), residual_window_days: int = 180):
        self.quantiles = tuple(quantiles)
        self.residual_window_days = residual_window_days
        self.offsets_: pd.DataFrame | None = None

    def fit(self, df: pd.DataFrame) -> SeasonalNaive:
        train = df[df[TARGET].notna()]
        last = train["settlement_date"].max()
        recent = train[
            train["settlement_date"] > last - pd.Timedelta(days=self.residual_window_days)
        ]
        resid = recent[TARGET] - seasonal_naive_point(recent)
        hour = recent["local_hour"].astype(int)
        frame = pd.DataFrame({"hour": hour, "resid": resid}).dropna()
        self.offsets_ = pd.DataFrame(
            {qcol(q): frame.groupby("hour")["resid"].quantile(q) for q in self.quantiles}
        )
        # P50 is the pure seasonal naive value; only the interval uses residuals.
        self.offsets_["p50"] = 0.0
        return self

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.offsets_ is None:
            raise RuntimeError("fit() first")
        point = seasonal_naive_point(df).to_numpy()
        off = self.offsets_.reindex(df["local_hour"].astype(int)).fillna(0.0)
        cols = [qcol(q) for q in self.quantiles]
        pred = point[:, None] + off[cols].to_numpy()
        return pd.DataFrame(_sort_quantiles(pred), columns=cols, index=df.index)

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        meta = {"quantiles": self.quantiles, "residual_window_days": self.residual_window_days}
        (path / "meta.json").write_text(json.dumps(meta))
        self.offsets_.to_csv(path / "offsets.csv")

    @classmethod
    def load(cls, path: Path) -> SeasonalNaive:
        meta = json.loads((path / "meta.json").read_text())
        m = cls(tuple(meta["quantiles"]), meta["residual_window_days"])
        m.offsets_ = pd.read_csv(path / "offsets.csv", index_col=0)
        return m


class QuantileLGBM:
    name: ClassVar[str] = "lgbm_quantile"

    def __init__(
        self,
        quantiles=(0.1, 0.5, 0.9),
        params: dict[str, Any] | None = None,
        target_mode: str = "delta_7d_mean",
        features: list[str] | None = None,
        calibrate: str = "none",
        calibration_days: int = 56,
        gap_days: int = 2,
    ):
        if target_mode not in {"level", "delta_7d_mean"}:
            raise ValueError(f"unknown target_mode {target_mode!r}")
        if calibrate not in {"none", "outer", "all"}:
            raise ValueError(f"unknown calibrate {calibrate!r}")
        self.quantiles = tuple(quantiles)
        self.params = dict(params or {})
        self.target_mode = target_mode
        self.features = list(features or FEATURES)
        self.calibrate = calibrate
        self.calibration_days = calibration_days
        self.gap_days = gap_days
        self.boosters_: dict[float, lgb.Booster] = {}
        self.shifts_: dict[float, float] = {q: 0.0 for q in self.quantiles}

    def _anchor(self, df: pd.DataFrame) -> np.ndarray:
        if self.target_mode == "level":
            return np.zeros(len(df))
        return df["price_7d_mean"].to_numpy(dtype=float)

    def _fit_boosters(self, train: pd.DataFrame) -> dict[float, lgb.Booster]:
        X = train[self.features]
        y = train[TARGET].to_numpy(dtype=float) - self._anchor(train)
        boosters = {}
        for q in self.quantiles:
            model = lgb.LGBMRegressor(objective="quantile", alpha=q, **self.params)
            model.fit(X, y)
            boosters[q] = model.booster_
        return boosters

    def _raw_predict(self, boosters, df: pd.DataFrame) -> np.ndarray:
        X = df[self.features]
        anchor = self._anchor(df)
        return np.column_stack([boosters[q].predict(X) + anchor for q in self.quantiles])

    def fit(self, df: pd.DataFrame) -> QuantileLGBM:
        train = df[df[TARGET].notna() & df["price_7d_mean"].notna()]
        self.shifts_ = {q: 0.0 for q in self.quantiles}
        if self.calibrate != "none":
            last = train["settlement_date"].max()
            cal_start = last - pd.Timedelta(days=self.calibration_days - 1)
            proper = train[train["settlement_date"] <= cal_start - pd.Timedelta(days=self.gap_days)]
            cal = train[train["settlement_date"] >= cal_start]
            raw = self._raw_predict(self._fit_boosters(proper), cal)
            y = cal[TARGET].to_numpy(dtype=float)
            for i, q in enumerate(self.quantiles):
                if self.calibrate == "outer" and q == 0.5:
                    continue
                # Shift so that exactly a fraction q of calibration targets fall below.
                self.shifts_[q] = float(np.quantile(y - raw[:, i], q))
        self.boosters_ = self._fit_boosters(train)
        return self

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        if not self.boosters_:
            raise RuntimeError("fit() first")
        raw = self._raw_predict(self.boosters_, df)
        pred = raw + np.array([self.shifts_[q] for q in self.quantiles])
        cols = [qcol(q) for q in self.quantiles]
        return pd.DataFrame(_sort_quantiles(pred), columns=cols, index=df.index)

    def feature_importance(self, quantile: float = 0.5) -> pd.Series:
        b = self.boosters_[quantile]
        imp = pd.Series(b.feature_importance("gain"), index=b.feature_name())
        return (imp / imp.sum()).sort_values(ascending=False)

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        meta = {
            "quantiles": self.quantiles,
            "params": self.params,
            "target_mode": self.target_mode,
            "features": self.features,
            "calibrate": self.calibrate,
            "calibration_days": self.calibration_days,
            "gap_days": self.gap_days,
            "shifts": {str(q): v for q, v in self.shifts_.items()},
        }
        (path / "meta.json").write_text(json.dumps(meta, indent=2))
        for q, b in self.boosters_.items():
            b.save_model(str(path / f"booster_{qcol(q)}.txt"))

    @classmethod
    def load(cls, path: Path) -> QuantileLGBM:
        meta = json.loads((path / "meta.json").read_text())
        m = cls(
            tuple(meta["quantiles"]),
            meta["params"],
            meta["target_mode"],
            meta["features"],
            meta.get("calibrate", "none"),
            meta.get("calibration_days", 56),
            meta.get("gap_days", 2),
        )
        m.shifts_ = {float(q): v for q, v in meta.get("shifts", {}).items()} or m.shifts_
        for q in m.quantiles:
            m.boosters_[q] = lgb.Booster(model_file=str(path / f"booster_{qcol(q)}.txt"))
        return m


MODEL_TYPES = {SeasonalNaive.name: SeasonalNaive, QuantileLGBM.name: QuantileLGBM}


def load_model(path: Path):
    kind = json.loads((path / "model_type.json").read_text())["model_type"]
    return MODEL_TYPES[kind].load(path)


def save_model(model, path: Path) -> None:
    model.save(path)
    (path / "model_type.json").write_text(json.dumps({"model_type": model.name}))
