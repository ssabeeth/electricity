import numpy as np
import pandas as pd
import pytest

from elecprice.modelling.metrics import evaluate
from elecprice.modelling.models import (
    QuantileLGBM,
    SeasonalNaive,
    load_model,
    save_model,
    seasonal_naive_point,
)
from tests.synthetic import synthetic_frame

FAST = {"n_estimators": 60, "learning_rate": 0.1, "num_leaves": 15, "verbose": -1, "n_jobs": 1}


@pytest.fixture(scope="module")
def frame():
    df = synthetic_frame(days=160)
    return df[df["price_7d_mean"].notna()].reset_index(drop=True)


def split(df, test_days=21):
    cut = df["settlement_date"].max() - pd.Timedelta(days=test_days)
    return df[df["settlement_date"] <= cut - pd.Timedelta(days=2)], df[df["settlement_date"] > cut]


def test_seasonal_naive_p50_is_last_weeks_price(frame):
    train, test = split(frame)
    p = SeasonalNaive().fit(train).predict(test)
    np.testing.assert_allclose(p["p50"], test["price_d7_same_period"])
    assert (p["p10"] <= p["p50"]).all() and (p["p50"] <= p["p90"]).all()


def test_seasonal_naive_falls_back_when_last_week_missing():
    df = pd.DataFrame(
        {
            "price_d7_same_period": [np.nan, 5.0],
            "price_d2_same_period": [3.0, 9.0],
            "price_7d_mean": [1, 1],
        }
    )
    assert list(seasonal_naive_point(df)) == [3.0, 5.0]


@pytest.mark.parametrize("target_mode", ["level", "delta_7d_mean"])
def test_lgbm_beats_naive_and_quantiles_are_ordered(frame, target_mode):
    train, test = split(frame)
    lgbm = QuantileLGBM(params=FAST, target_mode=target_mode).fit(train)
    p = lgbm.predict(test)
    assert (p["p10"] <= p["p50"]).all() and (p["p50"] <= p["p90"]).all()
    m_lgbm = evaluate(test["price_gbp_mwh"], p)
    m_naive = evaluate(test["price_gbp_mwh"], SeasonalNaive().fit(train).predict(test))
    assert m_lgbm["pinball_mean"] < m_naive["pinball_mean"]


def test_conformal_calibration_moves_coverage_towards_nominal(frame):
    train, test = split(frame)
    # A heavily regularised model underfits the spread; calibration must widen it.
    params = {**FAST, "n_estimators": 400, "min_child_samples": 5, "num_leaves": 63}
    raw = QuantileLGBM(params=params, calibrate="none").fit(train)
    cal = QuantileLGBM(params=params, calibrate="all", calibration_days=28).fit(train)
    cov_raw = evaluate(test["price_gbp_mwh"], raw.predict(test))["coverage"]
    cov_cal = evaluate(test["price_gbp_mwh"], cal.predict(test))["coverage"]
    assert abs(cov_cal - 0.8) < abs(cov_raw - 0.8) or abs(cov_cal - 0.8) < 0.05
    assert cal.shifts_[0.1] <= 0 <= cal.shifts_[0.9] or abs(cov_raw - 0.8) < 0.05


@pytest.mark.parametrize("cls", [SeasonalNaive, QuantileLGBM])
def test_save_load_round_trip(frame, tmp_path, cls):
    train, test = split(frame)
    model = cls(params=FAST, calibrate="all") if cls is QuantileLGBM else cls()
    model.fit(train)
    save_model(model, tmp_path / "m")
    loaded = load_model(tmp_path / "m")
    pd.testing.assert_frame_equal(model.predict(test), loaded.predict(test))


def test_invalid_options_rejected():
    with pytest.raises(ValueError):
        QuantileLGBM(target_mode="log")
    with pytest.raises(ValueError):
        QuantileLGBM(calibrate="sometimes")
