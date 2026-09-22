"""Walk-forward backtest with expanding training windows.

Each fold tests one calendar month. The models are refitted on every delivery
day up to ``test_start - gap_days`` (expanding window, no shuffling), which
mirrors what was knowable at the first test day's cutoff: at 09:00 on D-1, the
newest fully settled delivery day is D-2. A hard assertion checks that no
training target was realised after the first test cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from elecprice.logging_utils import get_logger
from elecprice.modelling.data import TARGET, ModelConfig
from elecprice.modelling.metrics import evaluate, qcol
from elecprice.modelling.models import QuantileLGBM, SeasonalNaive

log = get_logger(__name__)


@dataclass(frozen=True)
class Fold:
    index: int
    train_end: date
    test_start: date
    test_end: date

    @property
    def label(self) -> str:
        return f"{self.test_start:%Y-%m}"


def make_folds(last_date: date, first_test_month: str, gap_days: int) -> list[Fold]:
    months = pd.period_range(first_test_month, pd.Timestamp(last_date).to_period("M"), freq="M")
    folds = []
    for i, m in enumerate(months, start=1):
        start = m.start_time.date()
        end = min(m.end_time.date(), last_date)
        folds.append(Fold(i, start - timedelta(days=gap_days), start, end))
    return folds


def build_models(config: ModelConfig) -> list:
    return [
        SeasonalNaive(config.quantiles, **config.baseline),
        QuantileLGBM(
            config.quantiles,
            config.lightgbm,
            config.target_mode,
            calibrate=config.calibration.get("mode", "none"),
            calibration_days=config.calibration.get("days", 56),
            gap_days=config.backtest["gap_days"],
        ),
    ]


def assert_no_leakage(train: pd.DataFrame, test: pd.DataFrame) -> None:
    """Every training target must have been realised before the first test cutoff."""
    last_train_end = train["start_time_utc"].max() + pd.Timedelta(minutes=30)
    first_cutoff = test["cutoff_utc"].min()
    if last_train_end > first_cutoff:
        raise AssertionError(
            f"leakage: training data runs to {last_train_end}, "
            f"after the first test cutoff {first_cutoff}"
        )


@dataclass
class BacktestResult:
    config: ModelConfig
    folds: list[Fold]
    predictions: pd.DataFrame
    fold_metrics: pd.DataFrame
    importance: pd.Series = field(default_factory=pd.Series)

    def summary(self) -> pd.DataFrame:
        """Metrics over all folds, selection folds and hold-out folds, per model."""
        sel = self.config.backtest.get("selection_folds", 0)
        rows = []
        for model, g in self.predictions.groupby("model"):
            for scope, part in (
                ("all", g),
                ("selection", g[g["fold"] <= sel]),
                ("holdout", g[g["fold"] > sel]),
            ):
                if part.empty:
                    continue
                m = evaluate(part[TARGET], part, self.config.quantiles)
                rows.append({"model": model, "scope": scope, **m})
        out = pd.DataFrame(rows)
        base = out[out["model"] == SeasonalNaive.name].set_index("scope")["pinball_mean"]
        out["pinball_skill_vs_baseline"] = 1 - out["pinball_mean"] / out["scope"].map(base)
        return out


def run_backtest(df: pd.DataFrame, config: ModelConfig) -> BacktestResult:
    known = df[df[TARGET].notna()]
    last_date = known["settlement_date"].max().date()
    folds = make_folds(last_date, config.backtest["first_test_month"], config.backtest["gap_days"])
    preds, metrics = [], []
    importance = pd.Series(dtype=float)
    cols = [qcol(q) for q in config.quantiles]

    for fold in folds:
        train = df[df["settlement_date"].dt.date <= fold.train_end]
        test = df[
            (df["settlement_date"].dt.date >= fold.test_start)
            & (df["settlement_date"].dt.date <= fold.test_end)
        ]
        if test.empty or train.empty:
            continue
        assert_no_leakage(train, test)
        for model in build_models(config):
            model.fit(train)
            p = model.predict(test)
            frame = test[["settlement_date", "settlement_period", "start_time_utc", TARGET]].copy()
            frame[cols] = p[cols].to_numpy()
            frame["model"] = model.name
            frame["fold"] = fold.index
            preds.append(frame)
            m = evaluate(test[TARGET], p, config.quantiles)
            metrics.append({"fold": fold.index, "month": fold.label, "model": model.name, **m})
            if isinstance(model, QuantileLGBM):
                importance = model.feature_importance()
        log.info(
            "fold %2d %s train<=%s: %s",
            fold.index,
            fold.label,
            fold.train_end,
            ", ".join(
                f"{r['model']} pinball={r['pinball_mean']:.2f}"
                for r in metrics
                if r["fold"] == fold.index
            ),
        )

    return BacktestResult(
        config=config,
        folds=folds,
        predictions=pd.concat(preds, ignore_index=True),
        fold_metrics=pd.DataFrame(metrics),
        importance=importance,
    )
