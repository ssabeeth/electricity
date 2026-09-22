"""Open-Meteo Previous Runs API: weather *as it was forecast*, not as observed.

Docs: https://open-meteo.com/en/docs/previous-runs-api

``<var>_previous_dayN`` is "the value that was predicted N*24 hours before valid
time". We store each N as a separate vintage with a conservative
``available_at = valid_time - N*24h + 6h``: worst-case run initialisation time
plus a six-hour allowance for the run to be computed and published. The feature
build then picks, per valid hour, the latest vintage available at the decision
cutoff. ``previous_day1`` is *not* safe for most of the delivery day, so this
matters (see DECISIONS.md).

Locations and their feature weights live in ``dbt/seeds/weather_locations.csv``
so that Python and dbt share one definition.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from elecprice.config import get_settings
from elecprice.ingest import http
from elecprice.ingest.store import Dataset

URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
MODEL = "icon_seamless"
VARIABLES = ["temperature_2m", "wind_speed_100m", "shortwave_radiation", "cloud_cover"]
LEAD_DAYS = (1, 2)
PUBLICATION_DELAY = pd.Timedelta(hours=6)


def load_locations() -> pd.DataFrame:
    path = get_settings().dbt_project_dir / "seeds" / "weather_locations.csv"
    return pd.read_csv(path)


def fetch_weather(start: date, end: date) -> Any:
    locs = load_locations()
    hourly = [f"{v}_previous_day{n}" for n in LEAD_DAYS for v in VARIABLES]
    payload = http.get_json(
        URL,
        {
            "latitude": ",".join(f"{x:.2f}" for x in locs["latitude"]),
            "longitude": ",".join(f"{x:.2f}" for x in locs["longitude"]),
            "hourly": ",".join(hourly),
            "models": MODEL,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timezone": "GMT",
            "wind_speed_unit": "ms",
        },
    )
    responses = payload if isinstance(payload, list) else [payload]
    return {
        "model": MODEL,
        "location_ids": locs["location_id"].tolist(),
        "responses": responses,
    }


def normalise_weather(payload: Any) -> pd.DataFrame:
    frames = []
    for loc_id, resp in zip(payload["location_ids"], payload["responses"], strict=True):
        hourly = resp.get("hourly", {})
        if not hourly.get("time"):
            continue
        valid = pd.to_datetime(hourly["time"])  # GMT, naive
        for n in LEAD_DAYS:
            cols = {v: hourly.get(f"{v}_previous_day{n}") for v in VARIABLES}
            frame = pd.DataFrame({"valid_time": valid, **cols})
            frame = frame.dropna(subset=VARIABLES, how="all")
            if frame.empty:
                continue
            frame.insert(0, "location_id", loc_id)
            frame.insert(1, "lead_days", n)
            frame["available_at"] = frame["valid_time"] - pd.Timedelta(days=n) + PUBLICATION_DELAY
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["model"] = payload["model"]
    df["lead_days"] = df["lead_days"].astype("int8")
    for c in ("valid_time", "available_at"):
        df[c] = df[c].astype("datetime64[us]")
    for v in VARIABLES:
        df[v] = df[v].astype("float64")
    return df


DATASETS = [
    Dataset(
        "openmeteo",
        "weather_forecast",
        28,
        fetch_weather,
        normalise_weather,
        lookahead_days=2,
        min_start=date(2024, 2, 1),  # previous-runs archive starts mid-Feb 2024
        description="ICON forecast vintages (previous_day1/2) at GB locations",
    ),
]
