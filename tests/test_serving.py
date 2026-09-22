"""API and dashboard tests over outputs produced by the real pipeline writers."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from elecprice.battery.optimise import BatteryParams
from elecprice.battery.simulate import simulate, summarise
from elecprice.modelling.backtest import run_backtest
from elecprice.modelling.data import ModelConfig
from elecprice.modelling.runner import save_backtest
from elecprice.serving.api import create_app
from elecprice.serving.data import OutputStore
from tests.synthetic import synthetic_frame


@pytest.fixture(scope="module")
def outputs(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("outputs")
    df = synthetic_frame(days=120, start="2024-03-01")
    cfg = ModelConfig(
        quantiles=(0.1, 0.5, 0.9),
        target_mode="delta_7d_mean",
        calibration={"mode": "all", "days": 14},
        lightgbm={"n_estimators": 30, "learning_rate": 0.1, "verbose": -1, "n_jobs": 1},
        baseline={"residual_window_days": 30},
        backtest={"first_test_month": "2024-05", "gap_days": 2, "selection_folds": 1},
    )
    result = run_backtest(df, cfg)
    save_backtest(result, root / "backtest", run_id="test-run")
    params = BatteryParams()
    daily, schedules = simulate(result.predictions, params, workers=1)
    (root / "battery").mkdir()
    daily.to_parquet(root / "battery" / "daily.parquet", index=False)
    schedules.to_parquet(root / "battery" / "schedules.parquet", index=False)
    summarise(daily).to_csv(root / "battery" / "summary.csv", index=False)
    (root / "battery" / "params.json").write_text(json.dumps({"power_mw": 1.0}))
    return root


@pytest.fixture(scope="module")
def client(outputs) -> TestClient:
    return TestClient(create_app(OutputStore(outputs)))


def test_health_reports_outputs(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["outputs"] == {
        "live_forecasts": False,
        "backtest": True,
        "battery_simulation": True,
    }


def test_latest_forecast_falls_back_to_backtest(client):
    body = client.get("/forecast/latest").json()
    assert body["source"] == "backtest"
    assert len(body["points"]) == 48 and len(body["baseline"]) == 48
    p = body["points"][0]
    assert p["p10"] <= p["p50"] <= p["p90"]
    assert p["actual"] is not None


def test_forecast_for_date_and_404(client):
    assert client.get("/forecast/2024-05-10").status_code == 200
    assert client.get("/forecast/2020-01-01").status_code == 404


def test_forecast_range_limits(client):
    r = client.get("/forecast/range", params={"start": "2024-05-01", "end": "2024-05-03"})
    assert r.status_code == 200
    assert {x["model"] for x in r.json()} == {"lgbm_quantile", "seasonal_naive"}
    assert len(r.json()) == 2 * 3 * 48
    too_long = client.get("/forecast/range", params={"start": "2024-01-01", "end": "2024-03-01"})
    assert too_long.status_code == 422


def test_backtest_metrics_and_coverage(client):
    m = client.get("/backtest/metrics").json()
    scopes = {(r["model"], r["scope"]) for r in m["summary"]}
    assert ("lgbm_quantile", "holdout") in scopes and ("seasonal_naive", "all") in scopes
    assert m["meta"]["mlflow_run_id"] == "test-run"
    assert len(m["folds"]) == 2 * 2
    cov = client.get("/backtest/coverage", params={"window": 7}).json()
    assert all(0 <= c["coverage_daily"] <= 1 for c in cov)


def test_simulation_endpoints(client):
    s = client.get("/simulation/summary").json()
    rows = {r["strategy"]: r for r in s["rows"]}
    assert rows["perfect_foresight"]["capture_vs_perfect"] == pytest.approx(1.0)
    daily = pd.DataFrame(client.get("/simulation/daily").json())
    last = daily.sort_values("settlement_date").groupby("strategy").tail(1).set_index("strategy")
    assert last.loc["perfect_foresight", "cumulative_gbp"] == pytest.approx(
        rows["perfect_foresight"]["net_gbp"]
    )
    sched = client.get("/simulation/schedule/2024-05-10").json()
    assert {x["strategy"] for x in sched} == set(rows)
    assert client.get("/simulation/schedule/2020-01-01").status_code == 404


def test_live_endpoints_prefer_live_outputs(outputs, tmp_path):
    root = tmp_path / "outputs"
    import shutil

    shutil.copytree(outputs, root)
    (root / "live").mkdir()
    fc = pd.DataFrame(
        {
            "settlement_date": pd.Timestamp("2024-06-01"),
            "settlement_period": range(1, 49),
            "start_time_utc": pd.date_range("2024-05-31 23:00", periods=48, freq="30min"),
            "cutoff_utc": pd.Timestamp("2024-05-31 08:00"),
            "model": "lgbm_quantile",
            "model_version": "7",
            "created_at": pd.Timestamp("2024-05-31 08:10"),
            "p10": 50.0,
            "p50": 60.0,
            "p90": 70.0,
        }
    )
    fc.to_parquet(root / "live" / "forecasts.parquet", index=False)
    c = TestClient(create_app(OutputStore(root)))
    body = c.get("/forecast/latest").json()
    assert body["source"] == "live" and body["model_version"] == "7"
    assert c.get("/health").json()["champion_version"] == "7"
    assert c.get("/live/metrics").json() == {"forecast_daily": [], "battery_daily": []}


def test_dashboard_renders_every_tab(outputs, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("ELEC_API_URL", "inprocess")
    monkeypatch.setenv("ELEC_DATA_DIR", str(outputs.parent))
    # OutputStore reads $ELEC_DATA_DIR/outputs
    target = outputs.parent / "outputs"
    if not target.exists():
        target.symlink_to(outputs)
    app = Path(__file__).resolve().parents[1] / "src" / "elecprice" / "serving" / "dashboard.py"
    at = AppTest.from_file(str(app), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    labels = [m.label for m in at.metric]
    assert "Pinball skill vs baseline" in labels
    assert "Share of perfect foresight" in labels
