"""Experiment candidates only look back to settled days, and the adoption rule is the rule."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from elecprice.modelling import experiments as ex
from elecprice.modelling.data import TARGET


def _frame(days: int = 40, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2025-01-01")
    rows = []
    for d in range(days):
        date = start + pd.Timedelta(days=d)
        for p in range(48):
            t = date + pd.Timedelta(minutes=30 * p)
            rows.append(
                {
                    "settlement_date": date,
                    "settlement_period": p + 1,
                    "start_time_utc": t,
                    "local_hour": t.hour,
                    "residual_demand_mw": 20000 + 5000 * np.sin(p / 7) + rng.normal(0, 800),
                    "windfor_mw": 5000 + rng.normal(0, 500),
                    "emb_wind_mw": 1000.0,
                    "emb_solar_mw": max(0.0, 3000 * np.sin((p - 12) / 24 * np.pi)),
                    "ndf_demand_mw": 25000.0,
                    "price_7d_mean": 80.0,
                }
            )
    df = pd.DataFrame(rows)
    df[TARGET] = 20 + 3 * df["residual_demand_mw"] / 1000 + rng.normal(0, 5, len(df))
    return df


@pytest.mark.parametrize("func", ["profile", "merit"])
def test_candidates_ignore_prices_after_d_minus_2(func):
    df = _frame()
    day = pd.Timestamp("2025-01-30")

    def features(frame):
        cand = ex.add_candidates(frame)
        cols = ["price_profile_7d"] if func == "profile" else ["merit_slope", "merit_implied_rel"]
        return cand.loc[cand["settlement_date"] == day, cols].to_numpy()

    before = features(df)
    assert np.isfinite(before).all()
    # prices on D-1 and D itself are not settled at 09:00 on D-1: changing them must not matter
    late = df["settlement_date"] >= day - pd.Timedelta(days=1)
    changed = df.assign(**{TARGET: df[TARGET].where(~late, df[TARGET] + 500)})
    np.testing.assert_allclose(features(changed), before)
    # D-2 is settled, so changing it must matter
    d2 = df["settlement_date"] == day - pd.Timedelta(days=2)
    moved = df.assign(**{TARGET: df[TARGET].where(~d2, df[TARGET] + 500)})
    assert not np.allclose(features(moved), before)


def test_merit_order_recovers_the_curve():
    df = ex.add_candidates(_frame())
    last = df[df["settlement_date"] == df["settlement_date"].max()]
    np.testing.assert_allclose(last["merit_slope"], 3.0, atol=0.2)
    implied = last["merit_implied_rel"] + last["price_7d_mean"]
    expected = 20 + 3 * last["residual_demand_mw"] / 1000
    assert (implied - expected).abs().max() < 3


def test_block_bootstrap_interval_covers_the_mean_and_shrinks_with_signal():
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 1, 360)
    lo, hi = ex.block_bootstrap_mean(noise)
    assert lo < noise.mean() < hi
    lo2, _ = ex.block_bootstrap_mean(noise + 1.0)
    assert lo2 > 0


def test_adoption_needs_the_interval_the_folds_and_the_coverage():
    good = {"gain_interval": [0.01, 0.05], "folds_better": 10}
    assert ex.adopt(good, coverage=0.8)
    assert not ex.adopt({**good, "gain_interval": [-0.01, 0.05]}, coverage=0.8)
    assert not ex.adopt({**good, "folds_better": 8}, coverage=0.8)
    assert not ex.adopt(good, coverage=0.75)


def test_paired_comparison_on_identical_runs_is_exactly_zero():
    df = _frame(days=10)
    pred = df[["settlement_date", "settlement_period", TARGET]].assign(
        p10=df[TARGET] - 5, p50=df[TARGET] + 1, p90=df[TARGET] + 5, fold=1
    )
    cmp = ex.paired(pred, pred.copy(), (0.1, 0.5, 0.9))
    assert cmp["mean_daily_gain"] == 0.0
    assert cmp["gain_interval"] == [0.0, 0.0]
    assert cmp["folds_better"] == 0


FAST = {
    "n_estimators": 60,
    "learning_rate": 0.1,
    "num_leaves": 15,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "random_state": 0,
    "verbose": -1,
    "n_jobs": 1,
}


@pytest.fixture(scope="module")
def synth():
    from tests.synthetic import synthetic_frame

    df = synthetic_frame(days=160)
    df = df[df["price_7d_mean"].notna()].reset_index(drop=True)
    cut = df["settlement_date"].max() - pd.Timedelta(days=21)
    return df[df["settlement_date"] <= cut - pd.Timedelta(days=2)], df[df["settlement_date"] > cut]


@pytest.mark.parametrize(
    "name", ["naive_d2", "lgbm_point", "linear_quantile", "xgboost_quantile", "catboost_quantile"]
)
def test_comparison_models_give_ordered_calibrated_quantiles(synth, name):
    from elecprice.modelling import compare
    from elecprice.modelling.metrics import evaluate
    from elecprice.modelling.models import SeasonalNaive

    if name.startswith(("xgboost", "catboost")):
        pytest.importorskip(name.split("_")[0])
    train, test = synth
    cls = {
        "naive_d2": compare.NaiveD2,
        "lgbm_point": compare.PointLGBM,
        "linear_quantile": compare.LinearQuantile,
        "xgboost_quantile": compare.XGBQuantile,
        "catboost_quantile": compare.CatQuantile,
    }[name]
    model = cls() if name == "naive_d2" else cls(params=FAST, calibrate="all", calibration_days=28)
    p = model.fit(train).predict(test)
    assert (p["p10"] <= p["p50"]).all() and (p["p50"] <= p["p90"]).all()
    m = evaluate(test[TARGET], p)
    naive = evaluate(test[TARGET], SeasonalNaive().fit(train).predict(test))
    assert 0.5 < m["coverage"] <= 1.0
    if name == "naive_d2":
        np.testing.assert_allclose(p["p50"], test["price_d2_same_period"])
    else:
        assert m["pinball_mean"] < naive["pinball_mean"]


def test_variants_fit_and_predict(synth):
    train, test = synth
    for model in (
        ex.WeightedLGBM(params=FAST, calibrate="all", calibration_days=28, half_life_days=30),
        ex.WindowLGBM(params=FAST, calibrate="all", calibration_days=28, window_days=90),
        ex.GroupCalibratedLGBM(params=FAST, calibrate="all", calibration_days=28),
    ):
        p = model.fit(train).predict(test)
        assert (p["p10"] <= p["p90"]).all() and p.notna().all().all()
