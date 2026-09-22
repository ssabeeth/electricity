"""Run the battery arbitrage simulation over out-of-sample forecasts.

For every delivery day, each strategy builds a schedule from its *decision*
prices, and every schedule is then settled at the *actual* prices:

* ``perfect_foresight``  decides on actual prices: an upper bound nobody can reach
* ``forecast_lgbm``      decides on the LightGBM P50 forecast
* ``forecast_naive``     decides on the seasonal-naive forecast (same half-hour last week)
* ``naive_fixed``        price-blind rule: charge overnight, discharge at the
                         evening peak (lower bound)

The forecasts are the walk-forward backtest predictions, so each day's
schedule only uses information available at that day's 09:00 D-1 cutoff.
"""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from elecprice.battery.optimise import BatteryParams, fixed_rule_schedule, optimise_day, settle
from elecprice.config import UK_TZ, get_settings
from elecprice.logging_utils import get_logger

log = get_logger(__name__)

STRATEGIES = ("perfect_foresight", "forecast_lgbm", "forecast_naive", "naive_fixed")
STRATEGY_LABELS = {
    "perfect_foresight": "Perfect foresight (upper bound)",
    "forecast_lgbm": "LightGBM P50 forecast",
    "forecast_naive": "Seasonal naive forecast",
    "naive_fixed": "Fixed overnight/evening rule (lower bound)",
}


def day_frames(predictions: pd.DataFrame) -> pd.DataFrame:
    """Backtest predictions (long, one row per model) -> one row per period."""
    wide = predictions.pivot_table(
        index=["settlement_date", "settlement_period", "start_time_utc"],
        columns="model",
        values="p50",
    ).reset_index()
    wide.columns.name = None
    actual = predictions.drop_duplicates(["settlement_date", "settlement_period"])[
        ["settlement_date", "settlement_period", "price_gbp_mwh"]
    ]
    df = wide.merge(actual, on=["settlement_date", "settlement_period"], how="left")
    df = df.rename(columns={"lgbm_quantile": "p50_lgbm", "seasonal_naive": "p50_naive"})
    local = pd.to_datetime(df["start_time_utc"]).dt.tz_localize("UTC").dt.tz_convert(UK_TZ)
    df["local_hour"] = local.dt.hour + local.dt.minute / 60
    return df.sort_values("start_time_utc").reset_index(drop=True)


MIN_ACTUAL_SHARE = 0.9


def _prepare_day(day: pd.DataFrame) -> pd.DataFrame | None:
    """None if the day cannot be settled (incomplete day or actuals not yet known)."""
    day = day.sort_values("settlement_period").copy()
    if len(day) not in (46, 48, 50):
        return None
    if day["price_gbp_mwh"].notna().mean() < MIN_ACTUAL_SHARE:
        return None
    # Untraded periods (0.1% of the data) have no MID price; interpolate within the day.
    day["actual"] = day["price_gbp_mwh"].interpolate(limit_direction="both")
    for col in ("p50_lgbm", "p50_naive"):
        day[col] = day[col].interpolate(limit_direction="both").fillna(day["actual"].mean())
    return day


def decision_inputs(strategy: str, day: pd.DataFrame, params: BatteryParams) -> dict:
    """Decision prices (and any action windows) for a strategy. Never settlement prices,
    except for the perfect-foresight bound, which is its definition."""
    if strategy == "perfect_foresight":
        return {"prices": day["actual"].to_numpy()}
    if strategy == "forecast_lgbm":
        return {"prices": day["p50_lgbm"].to_numpy()}
    if strategy == "forecast_naive":
        return {"prices": day["p50_naive"].to_numpy()}
    if strategy == "naive_fixed":
        # Price-blind: the schedule depends only on the clock.
        return {"prices": np.full(len(day), np.nan), "local_hour": day["local_hour"].to_numpy()}
    raise KeyError(strategy)


def schedule_for(strategy: str, day: pd.DataFrame, params: BatteryParams):
    """(decision prices, schedule) for one strategy on one day."""
    inputs = decision_inputs(strategy, day, params)
    if strategy == "naive_fixed":
        return inputs["prices"], fixed_rule_schedule(inputs["local_hour"], params)
    return inputs["prices"], optimise_day(inputs["prices"], params)


def simulate_day(day: pd.DataFrame, params: BatteryParams) -> tuple[list[dict], list[pd.DataFrame]]:
    results, schedules = [], []
    date = day["settlement_date"].iloc[0]
    for strategy in STRATEGIES:
        prices, sched = schedule_for(strategy, day, params)
        st = settle(sched, day["actual"].to_numpy(), params)
        results.append({"settlement_date": date, "strategy": strategy, **asdict(st)})
        schedules.append(
            pd.DataFrame(
                {
                    "settlement_date": date,
                    "settlement_period": day["settlement_period"].to_numpy(),
                    "start_time_utc": day["start_time_utc"].to_numpy(),
                    "strategy": strategy,
                    "decision_price": prices,
                    "actual_price": day["actual"].to_numpy(),
                    "charge_mw": sched.charge_mw,
                    "discharge_mw": sched.discharge_mw,
                    "soc_mwh": sched.soc_mwh,
                }
            )
        )
    return results, schedules


def _simulate_chunk(args):
    days, params = args
    res, sch = [], []
    for day in days:
        r, s = simulate_day(day, params)
        res.extend(r)
        sch.extend(s)
    return res, sch


def simulate(
    predictions: pd.DataFrame, params: BatteryParams, workers: int = 4
) -> tuple[pd.DataFrame, pd.DataFrame]:
    frames = day_frames(predictions)
    days = [d for _, g in frames.groupby("settlement_date") if (d := _prepare_day(g)) is not None]
    chunks = [days[i :: max(1, workers)] for i in range(max(1, workers))]
    results, schedules = [], []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for r, s in pool.map(_simulate_chunk, [(c, params) for c in chunks if c]):
            results.extend(r)
            schedules.extend(s)
    daily = pd.DataFrame(results).sort_values(["settlement_date", "strategy"])
    sched = pd.concat(schedules, ignore_index=True).sort_values(["strategy", "start_time_utc"])
    return daily.reset_index(drop=True), sched.reset_index(drop=True)


def summarise(daily: pd.DataFrame) -> pd.DataFrame:
    days = daily["settlement_date"].nunique()
    g = daily.groupby("strategy").agg(
        net_gbp=("net_gbp", "sum"),
        revenue_gbp=("revenue_gbp", "sum"),
        degradation_gbp=("degradation_gbp", "sum"),
        cycles=("cycles", "sum"),
        discharged_mwh=("discharged_mwh", "sum"),
    )
    g["days"] = days
    g["net_gbp_per_mw_year"] = g["net_gbp"] / days * 365  # 1 MW battery
    g["cycles_per_day"] = g["cycles"] / days
    pf = g.loc["perfect_foresight", "net_gbp"]
    lo = g.loc["naive_fixed", "net_gbp"]
    g["capture_vs_perfect"] = g["net_gbp"] / pf
    # Share of the gap between the naive rule and perfect foresight that is closed.
    g["share_of_gap_closed"] = (g["net_gbp"] - lo) / (pf - lo)
    return g.reindex(list(STRATEGIES)).reset_index()


def battery_dir() -> Path:
    return get_settings().outputs_dir / "battery"


def run(params: BatteryParams | None = None, workers: int = 4) -> pd.DataFrame:
    params = params or BatteryParams.load()
    preds_path = get_settings().outputs_dir / "backtest" / "predictions.parquet"
    predictions = pd.read_parquet(preds_path)
    daily, sched = simulate(predictions, params, workers=workers)
    summary = summarise(daily)
    out = battery_dir()
    out.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(out / "daily.parquet", index=False)
    sched.to_parquet(out / "schedules.parquet", index=False)
    summary.to_csv(out / "summary.csv", index=False)
    (out / "params.json").write_text(json.dumps(asdict(params), indent=2))
    log.info("battery summary:\n%s", summary.round(3).to_string(index=False))
    return summary
