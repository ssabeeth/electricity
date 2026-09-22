from datetime import date
from itertools import pairwise

import pandas as pd
import pytest

from elecprice.modelling.backtest import assert_no_leakage, make_folds, run_backtest
from elecprice.modelling.data import ModelConfig
from tests.synthetic import synthetic_frame


def test_make_folds_are_monthly_expanding_with_gap():
    folds = make_folds(date(2025, 6, 10), "2025-03", gap_days=2)
    assert [f.label for f in folds] == ["2025-03", "2025-04", "2025-05", "2025-06"]
    assert folds[0].train_end == date(2025, 2, 27)
    assert folds[-1].test_end == date(2025, 6, 10)  # partial final month
    assert all(a.train_end < b.train_end for a, b in pairwise(folds))
    assert all(f.train_end < f.test_start for f in folds)


def test_assert_no_leakage_rejects_overlapping_training_data():
    df = synthetic_frame(days=20)
    test = df[df["settlement_date"] >= "2024-03-15"]
    ok_train = df[df["settlement_date"] <= "2024-03-13"]
    assert_no_leakage(ok_train, test)
    leaky_train = df[df["settlement_date"] <= "2024-03-14"]  # D-1 fully in training
    with pytest.raises(AssertionError, match="leakage"):
        assert_no_leakage(leaky_train, test)


def test_run_backtest_end_to_end_on_synthetic_data():
    df = synthetic_frame(days=150, start="2024-03-01")
    cfg = ModelConfig(
        quantiles=(0.1, 0.5, 0.9),
        target_mode="delta_7d_mean",
        calibration={"mode": "all", "days": 21},
        lightgbm={"n_estimators": 40, "learning_rate": 0.1, "verbose": -1, "n_jobs": 1},
        baseline={"residual_window_days": 60},
        backtest={"first_test_month": "2024-06", "gap_days": 2, "selection_folds": 1},
    )
    res = run_backtest(df, cfg)
    assert set(res.predictions["model"]) == {"seasonal_naive", "lgbm_quantile"}
    assert res.fold_metrics["fold"].nunique() == len(res.folds) == 2
    # Every prediction is for a date inside its fold's test window.
    for fold in res.folds:
        p = res.predictions[res.predictions["fold"] == fold.index]
        assert p["settlement_date"].min() >= pd.Timestamp(fold.test_start)
        assert p["settlement_date"].max() <= pd.Timestamp(fold.test_end)
    summary = res.summary()
    assert set(summary["scope"]) == {"all", "selection", "holdout"}
    lgbm = summary[(summary["model"] == "lgbm_quantile") & (summary["scope"] == "all")].iloc[0]
    assert lgbm["pinball_skill_vs_baseline"] > 0
