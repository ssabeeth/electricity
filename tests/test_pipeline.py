from __future__ import annotations

import stat
from datetime import date

import pandas as pd
import pytest

from elecprice.pipeline import store
from elecprice.pipeline.retrain import decide, judgement_fold


def test_decide_promotes_only_on_strictly_better_pinball():
    assert decide(5.0, 4.9, n=500, min_n=230).promote
    assert not decide(5.0, 5.0, n=500, min_n=230).promote  # ties keep production
    assert not decide(5.0, 5.1, n=500, min_n=230).promote
    assert not decide(5.0, 1.0, n=100, min_n=230).promote  # too little evidence
    assert not decide(5.0, None, n=500, min_n=230).promote
    assert decide(None, 7.0, n=0, min_n=230).promote  # no champion yet


def test_judgement_fold_starts_after_both_models_training_data():
    df = pd.DataFrame(
        {
            "settlement_date": pd.date_range("2024-01-01", periods=20, freq="D"),
            "price_gbp_mwh": 1.0,
        }
    )
    fold = judgement_fold(df, [date(2024, 1, 5), date(2024, 1, 12)], gap_days=2)
    assert fold["settlement_date"].min() == pd.Timestamp("2024-01-14")
    assert fold["settlement_date"].max() == pd.Timestamp("2024-01-20")


def test_tomorrow_uk_handles_timezones_and_clock_changes():
    from datetime import datetime

    from elecprice.pipeline.live import tomorrow_uk

    # 23:30 UTC on 21 Jun is 00:30 BST on 22 Jun, so "tomorrow" is 23 Jun.
    assert tomorrow_uk(datetime(2026, 6, 21, 23, 30)) == date(2026, 6, 23)
    assert tomorrow_uk(datetime.fromisoformat("2026-09-21T08:05:00+00:00")) == date(2026, 9, 22)
    assert tomorrow_uk(datetime(2026, 1, 10, 23, 30)) == date(2026, 1, 11)  # GMT


def test_upsert_replaces_matching_keys_and_is_world_readable(tmp_path):
    path = tmp_path / "out.parquet"
    store.upsert(path, pd.DataFrame({"k": [1, 2], "v": ["a", "b"]}), ["k"])
    merged = store.upsert(path, pd.DataFrame({"k": [2, 3], "v": ["B", "c"]}), ["k"])
    assert merged.set_index("k")["v"].to_dict() == {1: "a", 2: "B", 3: "c"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert store.read(tmp_path / "missing.parquet").empty


@pytest.mark.slow
def test_champion_challenger_and_daily_pipeline_end_to_end(
    fixture_warehouse, tmp_path, monkeypatch
):
    """Bootstrap -> challenger -> judged promotion -> forecast -> schedule -> monitor."""
    monkeypatch.setenv("ELEC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEC_DUCKDB_PATH", str(fixture_warehouse))
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")

    from mlflow.tracking import MlflowClient

    from elecprice.modelling import tracking
    from elecprice.modelling.data import ModelConfig
    from elecprice.pipeline import live
    from elecprice.pipeline.retrain import retrain

    cfg = ModelConfig.load()
    cfg = ModelConfig(
        quantiles=cfg.quantiles,
        target_mode=cfg.target_mode,
        calibration={"mode": "all", "days": 7},
        lightgbm={"n_estimators": 50, "learning_rate": 0.1, "verbose": -1, "n_jobs": 1},
        baseline={"residual_window_days": 14},
        backtest=cfg.backtest,
    )

    first = retrain(cfg)  # nothing registered: becomes champion
    assert first["new_alias"] == "champion" and not first["promoted"]
    second = retrain(cfg)  # champion exists, no challenger yet
    assert second["new_alias"] == "challenger"
    assert second["reason"] == "no challenger to judge"

    # Pretend both models were trained two weeks ago so a judgement fold exists.
    client = MlflowClient()
    for v in (first["new_version"], second["new_version"]):
        client.set_model_version_tag(tracking.REGISTERED_MODEL, v, "trained_through", "2024-03-24")
    third = retrain(cfg, min_eval_days=5)
    assert third["fold_start"] == "2024-03-26"
    assert third["fold_periods"] >= 5 * 46
    assert third["challenger_pinball"] is not None and third["champion_pinball"] is not None
    champion_now = client.get_model_version_by_alias(tracking.REGISTERED_MODEL, "champion").version
    expected = second["new_version"] if third["promoted"] else first["new_version"]
    assert str(champion_now) == str(expected)

    fc = live.forecast_day(date(2024, 4, 7))
    assert set(fc["model"]) == {"lgbm_quantile", "seasonal_naive"}
    assert len(fc) == 2 * 48
    assert (fc["p10"] <= fc["p50"]).all() and (fc["p50"] <= fc["p90"]).all()
    sched = live.schedule_day(date(2024, 4, 7))
    assert len(sched) == 48
    counts = live.monitor(lookback_days=100000)
    assert counts == {"forecast_days": 1, "battery_days": 1}
    daily = pd.read_parquet(tmp_path / "outputs" / "live" / "battery_daily.parquet")
    pf = daily.set_index("strategy").loc["perfect_foresight", "net_gbp"]
    assert pf >= daily.set_index("strategy").loc["forecast_lgbm", "net_gbp"] - 1e-6

    with pytest.raises(RuntimeError, match="No feature rows"):
        live.forecast_day(date(2024, 4, 20))  # outside the fixture calendar
