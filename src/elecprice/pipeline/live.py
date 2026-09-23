"""Daily production steps: forecast D+1, schedule the battery, monitor past days.

Run by the ``elec_daily_forecast`` DAG just after the 09:00 UK cutoff:

1. ``forecast_day(D)`` scores the champion model (and the seasonal-naive
   baseline) on D's rows of ``mart_features``. Those rows exist only once D's
   cutoff has passed, and the dbt point-in-time test has already checked them.
2. ``schedule_day(D)`` turns each model's P50 forecast into a battery schedule.
3. ``monitor()`` settles every past day that now has actual prices: live
   pinball / coverage / MAE, and each schedule's P&L against perfect foresight.

The champion comes from the MLflow registry, or from an exported model on disk
when ``ELEC_MODEL_DIR`` is set (the public track record uses one; see
``elec export-model`` and ``record_run.refit``).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from elecprice.battery.optimise import BatteryParams, Schedule, optimise_day, settle
from elecprice.config import UK_TZ, get_settings
from elecprice.logging_utils import get_logger
from elecprice.modelling import tracking
from elecprice.modelling.data import TARGET, ModelConfig, load_frame
from elecprice.modelling.metrics import evaluate
from elecprice.modelling.models import EXPORT_META, SeasonalNaive, load_model
from elecprice.modelling.runner import TRAINING_EXPERIMENT
from elecprice.pipeline import store

log = get_logger(__name__)

KEYS = ["settlement_date", "settlement_period"]
# Which forecast drives which battery strategy.
SCHEDULED_MODELS = {"lgbm_quantile": "forecast_lgbm", "seasonal_naive": "forecast_naive"}


def _paths() -> dict[str, Path]:
    out = get_settings().outputs_dir
    return {
        "forecasts": out / "live" / "forecasts.parquet",
        "schedules": out / "live" / "battery_schedules.parquet",
        "forecast_metrics": out / "live" / "forecast_daily_metrics.parquet",
        "battery_daily": out / "live" / "battery_daily.parquet",
    }


def tomorrow_uk(now: datetime | None = None) -> date:
    """The UK delivery day after ``now`` (naive datetimes are taken as UTC)."""
    ts = pd.Timestamp(now or datetime.now(UTC))
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
    return (ts.tz_convert(UK_TZ) + pd.Timedelta(days=1)).date()


def load_champion(model_dir: Path | None = None):
    """The model to forecast with, and its version.

    An exported model directory if one is given or ELEC_MODEL_DIR is set (its
    release name is the version); otherwise the MLflow registry's champion.
    """
    model_dir = model_dir or (Path(p) if (p := os.environ.get("ELEC_MODEL_DIR")) else None)
    if model_dir:
        meta = json.loads((model_dir / EXPORT_META).read_text())
        return load_model(model_dir), str(meta.get("release") or meta["version"])
    tracking.configure(TRAINING_EXPERIMENT)
    return tracking.load_registered(tracking.CHAMPION)


def forecast_day(delivery_date: date | None = None, model_dir: Path | None = None) -> pd.DataFrame:
    delivery_date = delivery_date or tomorrow_uk()
    d = pd.Timestamp(delivery_date)
    config = ModelConfig.load()
    history = load_frame(
        start=str(
            (d - pd.Timedelta(days=config.baseline.get("residual_window_days", 180) + 3)).date()
        ),
        end=str(delivery_date),
    )
    rows = history[history["settlement_date"] == d]
    if rows.empty:
        raise RuntimeError(
            f"No feature rows for {delivery_date}. Its cutoff (09:00 UK on the day before) "
            "may not have passed yet, or dbt has not been rebuilt since ingestion."
        )
    model, version = load_champion(model_dir)
    baseline = SeasonalNaive(config.quantiles, **config.baseline).fit(
        history[history["settlement_date"] <= d - pd.Timedelta(days=2)]
    )
    created = pd.Timestamp(datetime.now(UTC)).tz_localize(None).floor("s")
    frames = []
    for name, m, ver in (
        ("lgbm_quantile", model, str(version)),
        ("seasonal_naive", baseline, "baseline"),
    ):
        p = m.predict(rows)
        frames.append(
            rows[["settlement_date", "settlement_period", "start_time_utc", "cutoff_utc"]]
            .assign(model=name, model_version=ver, created_at=created)
            .join(p)
        )
    out = pd.concat(frames, ignore_index=True)
    store.upsert(_paths()["forecasts"], out, [*KEYS, "model"])
    log.info("forecast for %s written (%d rows, champion v%s)", delivery_date, len(out), version)
    return out


def schedule_day(delivery_date: date | None = None, params: BatteryParams | None = None):
    """A battery schedule from each model's P50, fixed before the day and settled later."""
    delivery_date = delivery_date or tomorrow_uk()
    params = params or BatteryParams.load()
    fc = store.read(_paths()["forecasts"])
    if fc.empty:
        raise RuntimeError(f"No forecast for {delivery_date}; run `elec forecast` first")
    fc = fc[pd.to_datetime(fc["settlement_date"]) == pd.Timestamp(delivery_date)]
    created = pd.Timestamp(datetime.now(UTC)).tz_localize(None).floor("s")
    frames = []
    for model, strategy in SCHEDULED_MODELS.items():
        day = fc[fc["model"] == model].sort_values("settlement_period")
        if day.empty:
            continue
        sched = optimise_day(day["p50"].to_numpy(), params)
        frames.append(
            day[["settlement_date", "settlement_period", "start_time_utc", "model_version"]].assign(
                strategy=strategy,
                decision_price=day["p50"].to_numpy(),
                charge_mw=sched.charge_mw,
                discharge_mw=sched.discharge_mw,
                soc_mwh=sched.soc_mwh,
                created_at=created,
            )
        )
        log.info(
            "%s schedule for %s: charge %.2f MWh, discharge %.2f MWh",
            strategy,
            delivery_date,
            sched.charge_mw.sum() * 0.5,
            sched.discharge_mw.sum() * 0.5,
        )
    if not frames or frames[0]["strategy"].iloc[0] != "forecast_lgbm":
        raise RuntimeError(f"No forecast for {delivery_date}; run `elec forecast` first")
    out = pd.concat(frames, ignore_index=True)
    store.upsert(_paths()["schedules"], out, [*KEYS, "strategy"])
    return out


def _actuals(start: date, end: date) -> pd.DataFrame:
    df = load_frame(start=str(start), end=str(end))
    return df[[*KEYS, TARGET]]


def monitor(params: BatteryParams | None = None, lookback_days: int = 60) -> dict[str, int]:
    """Settle past forecasts and schedules that now have actual prices."""
    params = params or BatteryParams.load()
    paths = _paths()
    fc = store.read(paths["forecasts"])
    if fc.empty:
        log.info("no live forecasts yet")
        return {"forecast_days": 0, "battery_days": 0}
    fc["settlement_date"] = pd.to_datetime(fc["settlement_date"])
    start = max(fc["settlement_date"].min(), pd.Timestamp.now() - pd.Timedelta(days=lookback_days))
    actual = _actuals(start.date(), date.today())
    joined = fc.merge(actual, on=KEYS, how="inner")

    metric_rows = []
    for (day, model), g in joined.groupby(["settlement_date", "model"]):
        if g[TARGET].notna().mean() < 0.9:
            continue  # day not settled yet
        m = evaluate(g[TARGET], g)
        metric_rows.append({"settlement_date": day, "model": model, **m})
    if metric_rows:
        store.upsert(
            paths["forecast_metrics"], pd.DataFrame(metric_rows), ["settlement_date", "model"]
        )

    sched = store.read(paths["schedules"])
    battery_rows = []
    if not sched.empty:
        sched["settlement_date"] = pd.to_datetime(sched["settlement_date"])
        sj = sched.merge(actual, on=KEYS, how="inner")
        for day, g in sj.groupby("settlement_date"):
            if g[TARGET].notna().mean() < 0.9:
                continue
            by_period = g.drop_duplicates("settlement_period").sort_values("settlement_period")
            prices = by_period[TARGET].interpolate(limit_direction="both").to_numpy()
            settled = {"perfect_foresight": optimise_day(prices, params)}
            for strategy, s in g.groupby("strategy"):
                s = s.sort_values("settlement_period")
                settled[strategy] = Schedule(
                    s["charge_mw"].to_numpy(), s["discharge_mw"].to_numpy(), s["soc_mwh"].to_numpy()
                )
            for strategy, schedule in settled.items():
                st = settle(schedule, prices, params)
                battery_rows.append({"settlement_date": day, "strategy": strategy, **st.__dict__})
    if battery_rows:
        store.upsert(
            paths["battery_daily"], pd.DataFrame(battery_rows), ["settlement_date", "strategy"]
        )
    counts = {
        "forecast_days": len({r["settlement_date"] for r in metric_rows}),
        "battery_days": len({r["settlement_date"] for r in battery_rows}),
    }
    log.info("monitor settled %s", counts)
    return counts


__all__ = ["forecast_day", "load_champion", "monitor", "schedule_day", "tomorrow_uk"]
