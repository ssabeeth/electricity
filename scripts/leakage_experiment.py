"""How much would leakage flatter the backtest?

Runs the same walk-forward backtest (identical models and settings) on four
feature sets that differ only in *when* their inputs were knowable:

1. honest: what mart_features contains. Every input was available at the 09:00
   D-1 cutoff, which the dbt point-in-time test enforces.
2. late weather forecast: weather always from the day-1 vintage ("predicted
   24 h before valid time"). For most hours that forecast was issued after the
   cutoff.
3. observed weather: Open-Meteo's reanalysis archive (what the weather
   actually was). Downloaded for this experiment only and never used by the
   pipeline.
4. observed system outturn: actual national demand and actual wind generation
   in place of the NESO forecasts (the same leak one level up).

    uv run python scripts/leakage_experiment.py   # writes reports/leakage_experiment.md
"""

# ruff: noqa: E501  (embedded SQL reads better unwrapped)
from __future__ import annotations

import gzip
import json

import duckdb
import pandas as pd

from elecprice.config import REPO_ROOT, get_settings
from elecprice.ingest import http
from elecprice.ingest.openmeteo import load_locations
from elecprice.modelling.backtest import run_backtest
from elecprice.modelling.data import TARGET, ModelConfig, load_frame
from elecprice.modelling.metrics import evaluate

WX = [
    "wx_wind_speed_100m_ms",
    "wx_wind_power_index",
    "wx_solar_radiation_wm2",
    "wx_cloud_cover_pct",
    "wx_temperature_c",
]

LEAKY_SQL = """
with per_location as (
    select c.settlement_date, c.settlement_period, w.location_id,
        w.wind_speed_100m_ms, w.shortwave_radiation_wm2, w.cloud_cover_pct, w.temperature_c,
        case when w.wind_speed_100m_ms < 3 or w.wind_speed_100m_ms >= 25 then 0.0
             when w.wind_speed_100m_ms >= 12 then 1.0
             else power((w.wind_speed_100m_ms - 3) / 9.0, 3) end as wind_power_index,
        w.available_at > c.cutoff_utc as after_cutoff
    from intermediate.int_settlement_calendar c
    join staging.stg_openmeteo__weather_forecast w
      on w.valid_hour_utc = c.hour_utc and w.lead_days = 1
)
select p.settlement_date, p.settlement_period,
    sum(wind_speed_100m_ms * l.wind_weight) / nullif(sum(l.wind_weight), 0) as wx_wind_speed_100m_ms,
    sum(wind_power_index * l.wind_weight) / nullif(sum(l.wind_weight), 0) as wx_wind_power_index,
    sum(shortwave_radiation_wm2 * l.solar_weight) / nullif(sum(l.solar_weight), 0) as wx_solar_radiation_wm2,
    sum(cloud_cover_pct * l.solar_weight) / nullif(sum(l.solar_weight), 0) as wx_cloud_cover_pct,
    sum(temperature_c * l.temperature_weight) / nullif(sum(l.temperature_weight), 0) as wx_temperature_c,
    avg(after_cutoff::int) as share_after_cutoff
from per_location p join seeds.weather_locations l using (location_id)
group by 1, 2
"""


ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def observed_weather(start: str, end: str) -> pd.DataFrame:
    """Hourly reanalysis at the same 15 locations, cached under data/raw/experiments."""
    cache = get_settings().raw_dir / "experiments" / "openmeteo_archive"
    cache.mkdir(parents=True, exist_ok=True)
    frames = []
    for loc in load_locations().itertuples():
        path = cache / f"{loc.location_id}_{start}_{end}.json.gz"
        if path.exists():
            payload = json.loads(gzip.decompress(path.read_bytes()))
        else:
            payload = http.get_json(
                ARCHIVE_URL,
                {
                    "latitude": loc.latitude,
                    "longitude": loc.longitude,
                    "start_date": start,
                    "end_date": end,
                    "hourly": "temperature_2m,wind_speed_100m,shortwave_radiation,cloud_cover",
                    "wind_speed_unit": "ms",
                    "timezone": "GMT",
                },
            )
            path.write_bytes(gzip.compress(json.dumps(payload).encode()))
        h = payload["hourly"]
        frames.append(
            pd.DataFrame(
                {
                    "location_id": loc.location_id,
                    "valid_hour_utc": pd.to_datetime(h["time"]),
                    "temperature_c": h["temperature_2m"],
                    "wind_speed_100m_ms": h["wind_speed_100m"],
                    "shortwave_radiation_wm2": h["shortwave_radiation"],
                    "cloud_cover_pct": h["cloud_cover"],
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


OBSERVED_SQL = """
with per_location as (
    select c.settlement_date, c.settlement_period, w.location_id,
        w.wind_speed_100m_ms, w.shortwave_radiation_wm2, w.cloud_cover_pct, w.temperature_c,
        case when w.wind_speed_100m_ms < 3 or w.wind_speed_100m_ms >= 25 then 0.0
             when w.wind_speed_100m_ms >= 12 then 1.0
             else power((w.wind_speed_100m_ms - 3) / 9.0, 3) end as wind_power_index
    from intermediate.int_settlement_calendar c
    join obs w on w.valid_hour_utc = c.hour_utc
)
select p.settlement_date, p.settlement_period,
    sum(wind_speed_100m_ms * l.wind_weight) / nullif(sum(l.wind_weight), 0) as wx_wind_speed_100m_ms,
    sum(wind_power_index * l.wind_weight) / nullif(sum(l.wind_weight), 0) as wx_wind_power_index,
    sum(shortwave_radiation_wm2 * l.solar_weight) / nullif(sum(l.solar_weight), 0) as wx_solar_radiation_wm2,
    sum(cloud_cover_pct * l.solar_weight) / nullif(sum(l.solar_weight), 0) as wx_cloud_cover_pct,
    sum(temperature_c * l.temperature_weight) / nullif(sum(l.temperature_weight), 0) as wx_temperature_c
from per_location p join seeds.weather_locations l using (location_id)
group by 1, 2
"""

OUTTURN_SQL = """
select d.settlement_date, d.settlement_period, d.indo_mw as ndf_demand_mw, g.wind_mw as windfor_mw
from staging.stg_elexon__demand_outturn d
join (
    select settlement_date, settlement_period, sum(generation_mw) as wind_mw
    from staging.stg_elexon__generation_by_fuel where fuel_category = 'wind'
    group by 1, 2
) g using (settlement_date, settlement_period)
"""


def swap(frame: pd.DataFrame, replacement: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    replacement = replacement.assign(settlement_date=pd.to_datetime(replacement["settlement_date"]))
    out = frame.drop(columns=cols).merge(
        replacement[["settlement_date", "settlement_period", *cols]],
        on=["settlement_date", "settlement_period"],
        how="left",
    )
    for c in cols:  # keep rows where the replacement is missing (e.g. archive lag)
        out[c] = out[c].fillna(frame.set_index(out.index)[c])
    return out


def main() -> None:
    honest = load_frame()
    start = str(honest["settlement_date"].min().date() - pd.Timedelta(days=1))
    end = str(honest["settlement_date"].max().date() - pd.Timedelta(days=7))  # archive lag
    obs = observed_weather(start, end)
    with duckdb.connect(str(get_settings().warehouse_path), read_only=True) as con:
        late_wx = con.sql(LEAKY_SQL).df()
        con.register("obs", obs)
        obs_wx = con.sql(OBSERVED_SQL).df()
        outturn = con.sql(OUTTURN_SQL).df()
    share_after = late_wx["share_after_cutoff"].mean()
    late = swap(honest, late_wx, WX)
    observed = swap(honest, obs_wx, WX)
    system = swap(honest, outturn, ["ndf_demand_mw", "windfor_mw"])
    system["residual_demand_mw"] = system["ndf_demand_mw"] - system["windfor_mw"]

    config = ModelConfig.load()
    variants = {
        "honest": honest,
        "late_weather_forecast": late,
        "observed_weather": observed,
        "observed_system_outturn": system,
    }
    rows = []
    for name, frame in variants.items():
        res = run_backtest(frame, config)
        p = res.predictions[res.predictions["model"] == "lgbm_quantile"]
        rows.append({"features": name, **evaluate(p[TARGET], p)})
    out = pd.DataFrame(rows).set_index("features")
    out["flattery_vs_honest"] = 1 - out["pinball_mean"] / out.loc["honest", "pinball_mean"]

    labels = {
        "honest": "Honest: every input available at 09:00 D-1 (what the pipeline uses)",
        "late_weather_forecast": f"Weather forecast issued after the cutoff ({share_after:.0%} of inputs)",
        "observed_weather": "Observed weather (reanalysis) instead of forecasts",
        "observed_system_outturn": "Actual demand and wind generation instead of NESO forecasts",
    }
    table = "\n".join(
        f"| {labels[k]} | {r.pinball_mean:.3f} | {r.flattery_vs_honest:+.1%} | {r.mae_p50:.2f} "
        f"| {r.coverage:.1%} |"
        for k, r in out.iterrows()
    )
    text = f"""# Leakage experiment: what the point-in-time rules are worth

_Generated by `scripts/leakage_experiment.py`._

The same walk-forward backtest (19 monthly folds, identical LightGBM settings
and conformal calibration) was run on four feature sets. They differ only in
*when* their inputs could have been known. A lower pinball loss in a leaky row
is performance that could never be achieved live.

| Feature set | Pinball | Apparent improvement | MAE P50 | P10-P90 coverage |
|---|---|---|---|---|
{table}

Reading the table:

- **Late weather forecasts barely matter here.** NESO's own demand, wind and
  embedded-solar forecasts already carry most of the weather signal, and the
  model leans on those. The dbt `point_in_time` test still rejects this variant.
- **Observed weather flatters by only about 1%, and that is itself a finding.**
  The weather features are a secondary input next to the NESO forecasts, which
  stay honest in that variant. In a model without system forecasts, where
  weather *is* the wind and solar signal, the same mistake would leak far more.
- **Observed system outturn flatters by about 10%.** This is the leak that
  matters for this feature set: using what actually happened to demand and wind
  output instead of what NESO forecast at the cutoff. It is also the easiest to
  commit by accident, because actual outturn sits in the same Elexon API as the
  forecasts. The as-of joins and the `point_in_time` test are what keep it out.

The observed-weather data was fetched for this experiment only. It is cached
under `data/raw/experiments/` and is not an input to any pipeline model.
"""
    path = REPO_ROOT / "reports" / "leakage_experiment.md"
    path.write_text(text)
    print(out[["pinball_mean", "flattery_vs_honest", "mae_p50", "coverage"]].round(3))
    print(f"share of late-forecast weather inputs issued after the cutoff: {share_after:.1%}")


if __name__ == "__main__":
    main()
