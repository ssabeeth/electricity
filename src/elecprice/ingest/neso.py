"""NESO data portal: embedded (distribution-connected) wind and solar forecasts.

Portal: https://www.neso.energy/data-portal/embedded-wind-and-solar-forecasts

The archive is one CKAN datastore resource per year. Each row is one forecast
vintage for one settlement period, stamped with ``Forecast_Datetime``.

Timezone gotcha, verified against the data: ``Forecast_Datetime`` is **UK local
time**, not GMT. There is no 01:12 vintage on the spring clock-change day, and
in summer the first period of the 04:12 vintage is the one containing 03:12 UTC.
``DATE_GMT``/``TIME_GMT`` are the GMT *end* of the period. We localise the
vintage time to Europe/London and resolve ambiguous autumn times to the later
instant, which is the conservative choice for point-in-time use.

Format change: from 2026-06-13 NESO publishes ``TIME_GMT`` as ``HH:MM`` and
vintages at irregular minutes (e.g. 06:53:03) whose first forecast period is
the *next* half-hour in UTC. Local vs UTC can no longer be verified for these
rows, so we take the later instant (UTC reading; identical in winter). Each row
records which basis was used in ``forecast_time_basis``.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

import pandas as pd

from elecprice.config import UK_TZ
from elecprice.ingest import http
from elecprice.ingest.store import Dataset

BASE = "https://api.neso.energy/api/3/action"
PACKAGE = "embedded-wind-and-solar-forecasts"
# Vintages issued in this local-time window are kept (see DECISIONS.md).
VINTAGE_WINDOW_LOCAL = ("04:00:00", "11:00:00")
HORIZON_DAYS = 3
COLUMNS = [
    "SETTLEMENT_DATE",
    "SETTLEMENT_PERIOD",
    "DATE_GMT",
    "TIME_GMT",
    "EMBEDDED_WIND_FORECAST",
    "EMBEDDED_WIND_CAPACITY",
    "EMBEDDED_SOLAR_FORECAST",
    "EMBEDDED_SOLAR_CAPACITY",
    "Forecast_Datetime",
]


@lru_cache(maxsize=1)
def archive_resources() -> dict[int, str]:
    """Map year -> datastore resource id for the yearly archive files."""
    pkg = http.get_json(f"{BASE}/package_show", {"id": PACKAGE})["result"]
    out: dict[int, str] = {}
    for res in pkg["resources"]:
        m = re.search(r"Archive (\d{4})", res.get("name", ""))
        if m and res.get("datastore_active"):
            out[int(m.group(1))] = res["id"]
    return out


def _sql_for(resource_id: str, days: list[date]) -> str:
    lo, hi = VINTAGE_WINDOW_LOCAL
    cols = ", ".join(f'"{c}"' for c in COLUMNS)
    windows = " OR ".join(
        f"(\"Forecast_Datetime\" >= '{d}T{lo}' AND \"Forecast_Datetime\" < '{d}T{hi}'"
        f" AND \"DATE_GMT\" <= '{d + timedelta(days=HORIZON_DAYS)}')"
        for d in days
    )
    return f'SELECT {cols} FROM "{resource_id}" WHERE {windows}'


def fetch_embedded(start: date, end: date) -> Any:
    resources = archive_resources()
    by_year: dict[int, list[date]] = {}
    d = start
    while d <= end:
        by_year.setdefault(d.year, []).append(d)
        d += timedelta(days=1)
    records: list[dict] = []
    for year, days in by_year.items():
        rid = resources.get(year)
        if rid is None:
            raise RuntimeError(f"No NESO embedded forecast archive resource for {year}")
        payload = http.get_json(f"{BASE}/datastore_search_sql", {"sql": _sql_for(rid, days)})
        if not payload.get("success"):
            raise RuntimeError(f"NESO SQL failed: {payload.get('error')}")
        records.extend(payload["result"]["records"])
    return {"records": records}


def normalise_embedded(payload: Any) -> pd.DataFrame:
    rows = payload.get("records", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)[COLUMNS]
    time_gmt = df["TIME_GMT"].astype(str)
    new_format = time_gmt.str.len() == 5  # "HH:MM" since 2026-06-13
    time_gmt = time_gmt.where(~new_format, time_gmt + ":00")

    issued_raw = pd.to_datetime(df["Forecast_Datetime"])
    as_uk_local = (
        issued_raw.dt.tz_localize(UK_TZ, ambiguous=False, nonexistent="shift_forward")
        .dt.tz_convert("UTC")
        .dt.tz_localize(None)
    )
    # The UTC reading is never earlier than the UK-local reading.
    issued_utc = as_uk_local.where(~new_format, issued_raw)
    period_end_gmt = pd.to_datetime(df["DATE_GMT"]) + pd.to_timedelta(time_gmt)
    return pd.DataFrame(
        {
            "forecast_issued_raw": issued_raw.astype("datetime64[us]"),
            "forecast_issued_at": issued_utc.astype("datetime64[us]"),
            "forecast_time_basis": new_format.map({False: "uk_local", True: "utc_assumed"}),
            "settlement_date": pd.to_datetime(df["SETTLEMENT_DATE"]).dt.date,
            "settlement_period": df["SETTLEMENT_PERIOD"].astype("int16"),
            "start_time": (period_end_gmt - pd.Timedelta(minutes=30)).astype("datetime64[us]"),
            "embedded_wind_mw": df["EMBEDDED_WIND_FORECAST"].astype("float64"),
            "embedded_wind_capacity_mw": df["EMBEDDED_WIND_CAPACITY"].astype("float64"),
            "embedded_solar_mw": df["EMBEDDED_SOLAR_FORECAST"].astype("float64"),
            "embedded_solar_capacity_mw": df["EMBEDDED_SOLAR_CAPACITY"].astype("float64"),
        }
    )


DATASETS = [
    Dataset(
        "neso",
        "embedded_forecast",
        7,
        fetch_embedded,
        normalise_embedded,
        description="Embedded wind/solar forecast vintages (issue-date chunks)",
    ),
]
