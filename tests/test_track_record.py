from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from elecprice.pipeline import record_run, track_record
from elecprice.pipeline.track_record import RecordError

DAY = date(2026, 9, 25)  # BST: delivery starts 2026-09-24 23:00 UTC


def _forecasts(day: date = DAY, created: str = "2026-09-24 09:20:00") -> pd.DataFrame:
    start = track_record.delivery_start_utc(day)
    rows = []
    for model, level in (("lgbm_quantile", 100.0), ("seasonal_naive", 90.0)):
        for sp in range(1, 49):
            rows.append(
                {
                    "settlement_date": pd.Timestamp(day),
                    "settlement_period": sp,
                    "start_time_utc": start + pd.Timedelta(minutes=30 * (sp - 1)),
                    "cutoff_utc": pd.Timestamp(day) - pd.Timedelta(hours=16),
                    "model": model,
                    "model_version": "1" if model == "lgbm_quantile" else "baseline",
                    "created_at": pd.Timestamp(created),
                    "p10": level - 10.123456,
                    "p50": level + sp / 3,
                    "p90": level + 20,
                }
            )
    return pd.DataFrame(rows)


def _schedules(day: date = DAY, created: str = "2026-09-24 09:20:05") -> pd.DataFrame:
    fc = _forecasts(day)
    rows = []
    for strategy, model in (
        ("forecast_lgbm", "lgbm_quantile"),
        ("forecast_naive", "seasonal_naive"),
    ):
        g = fc[fc["model"] == model]
        rows.append(
            g[["settlement_date", "settlement_period", "start_time_utc", "model_version"]].assign(
                strategy=strategy,
                decision_price=g["p50"],
                charge_mw=0.0,
                discharge_mw=0.0,
                soc_mwh=0.0,
                created_at=pd.Timestamp(created),
            )
        )
    return pd.concat(rows, ignore_index=True)


def test_delivery_start_is_uk_midnight_in_utc():
    # 25 Sep 2026 is in BST (UTC+1), so 00:00 UK is 23:00 UTC the day before.
    assert track_record.delivery_start_utc(date(2026, 9, 25)) == pd.Timestamp("2026-09-24 23:00")
    # 1 Dec 2026 is in GMT, so 00:00 UK is 00:00 UTC.
    assert track_record.delivery_start_utc(date(2026, 12, 1)) == pd.Timestamp("2026-12-01 00:00")


def test_a_day_is_written_once_and_never_replaced(tmp_path):
    assert track_record.record_day(tmp_path, DAY, _forecasts(), _schedules())
    path = tmp_path / "forecasts" / "2026-09-25.csv"
    first = path.read_text()
    assert len(pd.read_csv(path)) == 96
    assert len(pd.read_csv(tmp_path / "schedules" / "2026-09-25.csv")) == 96

    changed = _forecasts().assign(p50=0.0)
    assert not track_record.record_day(tmp_path, DAY, changed, _schedules())
    assert path.read_text() == first


def test_a_forecast_made_once_the_day_has_begun_is_refused(tmp_path):
    # One second before 23:00 UTC is still before delivery; 23:00 itself is not.
    ok = _forecasts(created="2026-09-24 22:59:59")
    assert track_record.record_day(
        tmp_path / "a", DAY, ok, _schedules(created="2026-09-24 22:59:59")
    )
    late = _forecasts(created="2026-09-24 23:00:00")
    with pytest.raises(RecordError, match="after delivery began"):
        track_record.record_day(tmp_path / "b", DAY, late, _schedules())
    assert not (tmp_path / "b" / "forecasts").exists()


def test_missing_schedules_are_refused(tmp_path):
    with pytest.raises(RecordError, match="missing"):
        track_record.record_day(tmp_path, DAY, _forecasts(), _schedules().iloc[0:0])


def test_the_record_round_trips_into_the_parquet_outputs(tmp_path):
    record, outputs = tmp_path / "record", tmp_path / "outputs"
    track_record.record_day(record, DAY, _forecasts(), _schedules())
    stale = outputs / "live" / "forecasts.parquet"
    stale.parent.mkdir(parents=True)
    _forecasts(date(2026, 1, 1), created="2025-12-31 09:00").to_parquet(stale)

    counts = track_record.materialise(record, outputs)
    assert counts["forecasts"] == 96 and counts["schedules"] == 96

    fc = pd.read_parquet(stale)
    # The record replaces whatever was there: the stale January day is gone.
    assert set(fc["settlement_date"]) == {pd.Timestamp(DAY)}
    original = _forecasts().sort_values(["model", "settlement_period"]).reset_index(drop=True)
    fc = fc.sort_values(["model", "settlement_period"]).reset_index(drop=True)
    for col in ("start_time_utc", "cutoff_utc", "created_at", "settlement_date"):
        assert (fc[col] == original[col]).all(), col
    assert fc["model_version"].tolist() == original["model_version"].tolist()
    # Prices are stored to four decimals.
    assert fc["p10"].iloc[0] == pytest.approx(89.8765, abs=1e-9)


def test_a_snapshot_is_copied_into_the_outputs(tmp_path):
    record, outputs = tmp_path / "record", tmp_path / "outputs"
    (record / "snapshot" / "backtest").mkdir(parents=True)
    (record / "snapshot" / "backtest" / "summary.csv").write_text("model\nx\n")
    track_record.materialise(record, outputs)
    assert (outputs / "backtest" / "summary.csv").read_text() == "model\nx\n"


def _write_scores(outputs):
    live = outputs / "live"
    live.mkdir(parents=True, exist_ok=True)
    d1, d2 = pd.Timestamp("2026-09-25"), pd.Timestamp("2026-09-26")
    pd.DataFrame(
        [
            {
                "settlement_date": d1,
                "model": "lgbm_quantile",
                "n": 48,
                "coverage": 0.75,
                "pinball_mean": 10.0,
                "mae_p50": 20.0,
            },
            {
                "settlement_date": d2,
                "model": "lgbm_quantile",
                "n": 46,
                "coverage": 0.5,
                "pinball_mean": 20.0,
                "mae_p50": 30.0,
            },
            {
                "settlement_date": d1,
                "model": "seasonal_naive",
                "n": 48,
                "coverage": 0.9,
                "pinball_mean": 20.0,
                "mae_p50": 40.0,
            },
            {
                "settlement_date": d2,
                "model": "seasonal_naive",
                "n": 46,
                "coverage": 0.9,
                "pinball_mean": 20.0,
                "mae_p50": 40.0,
            },
        ]
    ).to_parquet(live / "forecast_daily_metrics.parquet")
    pd.DataFrame(
        [
            {"settlement_date": d1, "strategy": "forecast_lgbm", "net_gbp": 100.0},
            {"settlement_date": d2, "strategy": "forecast_lgbm", "net_gbp": 50.0},
            {"settlement_date": d1, "strategy": "forecast_naive", "net_gbp": 30.0},
            {"settlement_date": d2, "strategy": "forecast_naive", "net_gbp": -10.0},
            {"settlement_date": d1, "strategy": "perfect_foresight", "net_gbp": 200.0},
            {"settlement_date": d2, "strategy": "perfect_foresight", "net_gbp": 100.0},
        ]
    ).to_parquet(live / "battery_daily.parquet")


def test_totals_are_pooled_over_half_hours(tmp_path):
    record, outputs = tmp_path / "record", tmp_path / "outputs"
    track_record.record_day(record, DAY, _forecasts(), _schedules())
    _write_scores(outputs)
    track_record.write_scores(record, outputs)
    s = track_record.summarise(record)

    assert s["days_forecast"] == 1 and s["days_settled"] == 2
    lg = s["models"]["lgbm_quantile"]
    # Coverage: (0.75 * 48 + 0.5 * 46) / 94 = (36 + 23) / 94 = 59 / 94.
    assert lg["coverage"] == pytest.approx(59 / 94)
    assert lg["half_hours"] == 94
    # Pinball: (10 * 48 + 20 * 46) / 94 = 1400 / 94; baseline is 20 on both days.
    assert lg["pinball_mean"] == pytest.approx(1400 / 94)
    assert s["pinball_skill_vs_naive"] == pytest.approx(1 - (1400 / 94) / 20)
    # Battery: 150 and 20 against a perfect-foresight 300.
    assert s["net_gbp"] == {
        "forecast_lgbm": 150.0,
        "forecast_naive": 20.0,
        "perfect_foresight": 300.0,
    }
    assert s["capture_vs_perfect"]["forecast_lgbm"] == pytest.approx(0.5)
    assert s["capture_vs_perfect"]["forecast_naive"] == pytest.approx(20 / 300)


def test_readme_reports_the_totals_and_the_model(tmp_path):
    record, outputs = tmp_path / "record", tmp_path / "outputs"
    track_record.record_day(record, DAY, _forecasts(), _schedules())
    _write_scores(outputs)
    track_record.write_scores(record, outputs)
    model = {"version": "1", "trained_through": "2026-09-21", "exported_at": "2026-09-24T00:10:00Z"}
    text = track_record.write_readme(record, "https://example.org/repo", model)
    assert text == (record / "README.md").read_text()
    assert "trained on delivery days up to 2026-09-21" in text
    assert f"P10–P90 coverage: {59 / 94:.1%} of 94 half-hours" in text
    assert "| Scheduled on the LightGBM forecast | £150.00 | 50.0% |" in text
    assert "| Perfect foresight (upper bound) | £300.00 | — |" in text


def test_daily_run_forecasts_once_then_only_settles(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEC_DATA_DIR", str(tmp_path / "data"))
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    export = {
        "version": "1",
        "trained_through": "2026-09-21",
        "exported_at": "2026-09-24T00:10:00Z",
        "sha256": {"booster.txt": "abc"},
        "run_id": "not published",
    }
    (model_dir / "export.json").write_text(json.dumps(export))
    monkeypatch.setenv("ELEC_MODEL_DIR", str(model_dir))
    calls = []
    monkeypatch.setattr(
        record_run, "forecast_day", lambda d: calls.append(("fc", d)) or _forecasts(d)
    )
    monkeypatch.setattr(
        record_run, "schedule_day", lambda d: calls.append(("sc", d)) or _schedules(d)
    )
    monkeypatch.setattr(record_run, "monitor", lambda: {"forecast_days": 0, "battery_days": 0})

    record = tmp_path / "record"
    after_cutoff = pd.Timestamp("2026-09-24 09:20", tz="UTC")
    first = record_run.run(record, "https://example.org/repo", now=after_cutoff)
    assert first["delivery_date"] == "2026-09-25"
    assert first["recorded"] and calls == [("fc", DAY), ("sc", DAY)]
    second = record_run.run(record, "https://example.org/repo", now=after_cutoff)
    assert not second["recorded"] and len(calls) == 2  # never re-forecast a published day

    published = json.loads((record / "model.json").read_text())
    assert published == {
        k: export[k] for k in ("version", "trained_through", "exported_at", "sha256")
    }
    assert "Days forecast: 1 (2026-09-25 to 2026-09-25)" in (record / "README.md").read_text()


def test_an_early_run_settles_but_waits_for_the_cutoff(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ELEC_MODEL_DIR", raising=False)
    monkeypatch.setattr(record_run, "forecast_day", lambda d: pytest.fail("forecast too early"))
    settled = []
    monkeypatch.setattr(record_run, "monitor", lambda: settled.append(1) or {"forecast_days": 0})
    # The cutoff for 25 Sep is 09:00 BST on 24 Sep, which is 08:00 UTC.
    early = pd.Timestamp("2026-09-24 07:59", tz="UTC")
    out = record_run.run(tmp_path / "record", "https://example.org/repo", now=early)
    assert out["delivery_date"] == "2026-09-25" and not out["recorded"]
    assert settled == [1]
    assert (tmp_path / "record" / "README.md").exists()
