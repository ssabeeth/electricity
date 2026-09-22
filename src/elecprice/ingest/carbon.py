"""National Grid ESO / NESO Carbon Intensity API. Free, no key required.

Docs: https://carbon-intensity.github.io/api-definitions/

Only ``actual`` intensity is used as a feature (lagged, for periods that ended
before the cutoff). The API's ``forecast`` field has no issue time, so it cannot
be used point-in-time; it is stored for reference only.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from elecprice.ingest import http
from elecprice.ingest.store import Dataset

BASE = "https://api.carbonintensity.org.uk"


def fetch_intensity(start: date, end: date) -> Any:
    frm = f"{start.isoformat()}T00:00Z"
    to = f"{(end + timedelta(days=1)).isoformat()}T00:00Z"
    return http.get_json(f"{BASE}/intensity/{frm}/{to}")


def normalise_intensity(payload: Any) -> pd.DataFrame:
    rows = payload.get("data", [])
    if not rows:
        return pd.DataFrame()
    df = pd.json_normalize(rows)
    out = pd.DataFrame(
        {
            "start_time": pd.to_datetime(df["from"], utc=True).dt.tz_localize(None),
            "end_time": pd.to_datetime(df["to"], utc=True).dt.tz_localize(None),
            "intensity_forecast": pd.to_numeric(df["intensity.forecast"], errors="coerce"),
            "intensity_actual": pd.to_numeric(df["intensity.actual"], errors="coerce"),
            "intensity_index": df["intensity.index"],
        }
    )
    for c in ("start_time", "end_time"):
        out[c] = out[c].astype("datetime64[us]")
    return out


DATASETS = [
    Dataset(
        "carbon",
        "intensity",
        28,
        fetch_intensity,
        normalise_intensity,
        description="National carbon intensity (gCO2/kWh)",
    ),
]
