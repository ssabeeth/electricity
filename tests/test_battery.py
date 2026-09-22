import numpy as np
import pandas as pd
import pytest

from elecprice.battery.optimise import (
    DT_HOURS,
    BatteryParams,
    fixed_rule_schedule,
    optimise_day,
    settle,
)
from elecprice.battery.simulate import decision_inputs, schedule_for, simulate, summarise

P = BatteryParams()  # 1 MW / 2 MWh, 90% RTE, empty at start and end of day


def check_physics(sched, p=P):
    assert (sched.charge_mw >= -1e-9).all() and (sched.charge_mw <= p.power_mw + 1e-9).all()
    assert (sched.discharge_mw >= -1e-9).all() and (sched.discharge_mw <= p.power_mw + 1e-9).all()
    assert (sched.soc_mwh >= p.soc_min - 1e-6).all() and (sched.soc_mwh <= p.soc_max + 1e-6).all()
    assert sched.soc_mwh[-1] == pytest.approx(p.soc_initial, abs=1e-6)
    # State of charge follows from the flows.
    soc = p.soc_initial + np.cumsum(
        p.eta_charge * sched.charge_mw * DT_HOURS - sched.discharge_mw * DT_HOURS / p.eta_discharge
    )
    np.testing.assert_allclose(soc, sched.soc_mwh, atol=1e-6)
    # Never charges and discharges in the same period.
    assert not np.any((sched.charge_mw > 1e-6) & (sched.discharge_mw > 1e-6))


def test_simple_spread_is_captured_optimally():
    prices = np.r_[np.full(24, 20.0), np.full(24, 120.0)]
    sched = optimise_day(prices, P)
    check_physics(sched)
    st = settle(sched, prices, P)
    # Cheap half fills 2 MWh of storage, expensive half empties it; cycle limit 1.5 is slack.
    charged_grid = P.energy_mwh / P.eta_charge
    sold_grid = P.energy_mwh * P.eta_discharge
    expected = 120 * sold_grid - 20 * charged_grid - P.degradation_cost_gbp_per_mwh * sold_grid
    assert st.net_gbp == pytest.approx(expected, rel=1e-6)
    assert st.cycles == pytest.approx(1.0)


def test_flat_prices_mean_no_trading():
    sched = optimise_day(np.full(48, 75.0), P)
    assert sched.charge_mw.sum() == pytest.approx(0) and sched.discharge_mw.sum() == pytest.approx(
        0
    )


def test_cycle_limit_is_respected():
    prices = np.tile(np.r_[np.full(6, 0.0), np.full(6, 300.0)], 4)  # four spreads a day
    p = BatteryParams(max_cycles_per_day=1.5)
    sched = optimise_day(prices, p)
    check_physics(sched, p)
    assert settle(sched, prices, p).cycles <= 1.5 + 1e-6


def test_very_negative_prices_never_charge_and_discharge_together():
    prices = np.r_[np.full(10, 50.0), np.full(28, -900.0), np.full(10, 50.0)]
    sched = optimise_day(prices, P)
    check_physics(sched)


@pytest.mark.parametrize("seed", range(5))
def test_perfect_foresight_dominates_forecast_driven(seed):
    rng = np.random.default_rng(seed)
    actual = 80 + 40 * np.sin(np.linspace(0, 2 * np.pi, 48) + seed) + rng.normal(0, 25, 48)
    forecast = actual + rng.normal(0, 30, 48)
    pf = settle(optimise_day(actual, P), actual, P).net_gbp
    fc = settle(optimise_day(forecast, P), actual, P).net_gbp
    assert pf >= fc - 1e-6


def test_forecast_schedule_is_independent_of_actual_prices():
    """No leakage: the decision only sees the forecast; actuals only enter settlement."""
    day = make_day(seed=3)
    inputs_a = decision_inputs("forecast_lgbm", day, P)
    day_b = day.assign(actual=day["actual"] * 3 - 50)
    inputs_b = decision_inputs("forecast_lgbm", day_b, P)
    np.testing.assert_array_equal(inputs_a["prices"], inputs_b["prices"])
    sa = optimise_day(inputs_a["prices"], P)
    sb = optimise_day(inputs_b["prices"], P)
    np.testing.assert_allclose(sa.net_export_mw, sb.net_export_mw)


def test_naive_rule_only_acts_in_its_windows_and_ignores_prices():
    day = make_day(seed=1)
    _, sched = schedule_for("naive_fixed", day, P)
    check_physics(sched)
    h = day["local_hour"].to_numpy()
    assert (sched.charge_mw[(h < 1) | (h >= 5)] < 1e-9).all()
    assert (sched.discharge_mw[(h < 16.5) | (h >= 19.5)] < 1e-9).all()
    assert settle(sched, day["actual"].to_numpy(), P).cycles == pytest.approx(1.0)
    # Charges from the start of the window: first four half-hours at full power.
    np.testing.assert_allclose(sched.charge_mw[2:6], 1.0)
    _, again = schedule_for("naive_fixed", day.assign(actual=-day["actual"]), P)
    np.testing.assert_array_equal(sched.net_export_mw, again.net_export_mw)


def test_fixed_rule_with_windows_too_short_stays_idle():
    p = BatteryParams(naive_charge_window=(1.0, 5.0), naive_discharge_window=(17.0, 17.5))
    sched = fixed_rule_schedule(np.arange(48) / 2, p)
    assert sched.charge_mw.sum() == 0 and sched.discharge_mw.sum() == 0


def make_day(seed=0, date="2025-06-02"):
    rng = np.random.default_rng(seed)
    t = pd.date_range(f"{date} 00:00", periods=48, freq="30min") - pd.Timedelta(hours=1)
    actual = 70 + 30 * np.sin(np.linspace(-np.pi / 2, 3 * np.pi / 2, 48)) + rng.normal(0, 10, 48)
    return pd.DataFrame(
        {
            "settlement_date": pd.Timestamp(date),
            "settlement_period": range(1, 49),
            "start_time_utc": t,
            "local_hour": np.arange(48) / 2,
            "actual": actual,
            "price_gbp_mwh": actual,
            "p50_lgbm": actual + rng.normal(0, 8, 48),
            "p50_naive": actual + rng.normal(0, 25, 48),
        }
    )


def test_simulate_and_summarise_on_synthetic_predictions():
    frames = []
    for i, date in enumerate(["2025-06-02", "2025-06-03", "2025-06-04"]):
        day = make_day(seed=i, date=date)
        for model, col in (("lgbm_quantile", "p50_lgbm"), ("seasonal_naive", "p50_naive")):
            frames.append(
                day[
                    ["settlement_date", "settlement_period", "start_time_utc", "price_gbp_mwh"]
                ].assign(model=model, p50=day[col])
            )
    preds = pd.concat(frames, ignore_index=True)
    daily, schedules = simulate(preds, P, workers=1)
    assert daily["settlement_date"].nunique() == 3
    assert set(daily["strategy"]) == {
        "perfect_foresight",
        "forecast_lgbm",
        "forecast_naive",
        "naive_fixed",
    }
    wide = daily.pivot_table(index="settlement_date", columns="strategy", values="net_gbp")
    for col in ("forecast_lgbm", "forecast_naive", "naive_fixed"):
        assert (wide["perfect_foresight"] >= wide[col] - 1e-6).all()
    s = summarise(daily).set_index("strategy")
    assert s.loc["perfect_foresight", "capture_vs_perfect"] == pytest.approx(1.0)
    assert s.loc["naive_fixed", "share_of_gap_closed"] == pytest.approx(0.0)
    assert len(schedules) == 3 * 4 * 48


def test_params_load_from_config():
    p = BatteryParams.load()
    assert p.power_mw == 1.0 and p.energy_mwh == 2.0
    assert p.eta_charge * p.eta_discharge == pytest.approx(0.9)
