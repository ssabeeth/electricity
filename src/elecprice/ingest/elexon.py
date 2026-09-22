"""Elexon Insights Solution (BMRS) API. Free, no key required.

Docs: https://data.elexon.co.uk/bmrs/api/v1 (Swagger at /swagger/index.html)

Datasets used:

* ``MID``      Market Index Data, half-hourly price and volume. Forecast target.
* ``INDO/ITSDO`` initial national / transmission demand outturn.
* ``FUELHH``   half-hourly generation outturn by fuel type.
* ``NDF``      NESO national demand forecast, with ``publishTime`` per vintage.
* ``WINDFOR``  NESO wind generation forecast (hourly), with ``publishTime``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from elecprice.ingest import http
from elecprice.ingest.store import Dataset

BASE = "https://data.elexon.co.uk/bmrs/api/v1"

# Forecast vintages are only needed around the 09:00 UK decision cutoff.
# 04:00-11:00 UTC covers the cutoff in GMT and BST, including vintages issued
# after it (which the point-in-time test must reject). See DECISIONS.md.
VINTAGE_WINDOW_UTC = ("04:00", "11:00")


def _iso(d: date) -> str:
    return f"{d.isoformat()}T00:00Z"


def _data(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        return payload.get("data", [])
    return payload


def _ts(s: pd.Series) -> pd.Series:
    """ISO-8601 UTC strings -> naive UTC timestamps (the warehouse convention)."""
    return pd.to_datetime(s, utc=True).dt.tz_localize(None).astype("datetime64[us]")


def _frame(rows: list[dict], columns: dict[str, str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=list(columns.values()))
    return df[list(columns)].rename(columns=columns)


# --- MID: market index price -------------------------------------------------


def fetch_mid(start: date, end: date) -> Any:
    return http.get_json(
        f"{BASE}/balancing/pricing/market-index",
        {
            "from": _iso(start),
            "to": _iso(end + timedelta(days=1)),
            "dataProviders": "APXMIDP",
            "format": "json",
        },
    )


def normalise_mid(payload: Any) -> pd.DataFrame:
    df = _frame(
        _data(payload),
        {
            "startTime": "start_time",
            "dataProvider": "data_provider",
            "settlementDate": "settlement_date",
            "settlementPeriod": "settlement_period",
            "price": "price",
            "volume": "volume",
        },
    )
    if df.empty:
        return df
    return df.assign(
        start_time=_ts(df["start_time"]),
        settlement_date=pd.to_datetime(df["settlement_date"]).dt.date,
        settlement_period=df["settlement_period"].astype("int16"),
        price=df["price"].astype("float64"),
        volume=df["volume"].astype("float64"),
    )


# --- Demand outturn ----------------------------------------------------------


def fetch_demand_outturn(start: date, end: date) -> Any:
    return http.get_json(
        f"{BASE}/demand/outturn",
        {
            "settlementDateFrom": start.isoformat(),
            "settlementDateTo": end.isoformat(),
            "format": "json",
        },
    )


def normalise_demand_outturn(payload: Any) -> pd.DataFrame:
    df = _frame(
        _data(payload),
        {
            "publishTime": "publish_time",
            "startTime": "start_time",
            "settlementDate": "settlement_date",
            "settlementPeriod": "settlement_period",
            "initialDemandOutturn": "indo_mw",
            "initialTransmissionSystemDemandOutturn": "itsdo_mw",
        },
    )
    if df.empty:
        return df
    return df.assign(
        publish_time=_ts(df["publish_time"]),
        start_time=_ts(df["start_time"]),
        settlement_date=pd.to_datetime(df["settlement_date"]).dt.date,
        settlement_period=df["settlement_period"].astype("int16"),
        indo_mw=df["indo_mw"].astype("float64"),
        itsdo_mw=df["itsdo_mw"].astype("float64"),
    )


# --- FUELHH: generation by fuel type -----------------------------------------


def fetch_fuelhh(start: date, end: date) -> Any:
    return http.get_json(
        f"{BASE}/datasets/FUELHH",
        {
            "settlementDateFrom": start.isoformat(),
            "settlementDateTo": end.isoformat(),
            "format": "json",
        },
    )


def normalise_fuelhh(payload: Any) -> pd.DataFrame:
    df = _frame(
        _data(payload),
        {
            "publishTime": "publish_time",
            "startTime": "start_time",
            "settlementDate": "settlement_date",
            "settlementPeriod": "settlement_period",
            "fuelType": "fuel_type",
            "generation": "generation_mw",
        },
    )
    if df.empty:
        return df
    return df.assign(
        publish_time=_ts(df["publish_time"]),
        start_time=_ts(df["start_time"]),
        settlement_date=pd.to_datetime(df["settlement_date"]).dt.date,
        settlement_period=df["settlement_period"].astype("int16"),
        generation_mw=df["generation_mw"].astype("float64"),
    )


# --- NDF: day-ahead national demand forecast ---------------------------------


def fetch_ndf(start: date, end: date) -> Any:
    """One request per publish day; the API caps publish windows at one day."""
    lo, hi = VINTAGE_WINDOW_UTC
    rows: list[dict] = []
    d = start
    while d <= end:
        payload = http.get_json(
            f"{BASE}/datasets/NDF",
            {
                "publishDateTimeFrom": f"{d.isoformat()}T{lo}Z",
                "publishDateTimeTo": f"{d.isoformat()}T{hi}Z",
                "format": "json",
            },
        )
        rows.extend(_data(payload))
        d += timedelta(days=1)
    return {"data": rows}


def normalise_ndf(payload: Any) -> pd.DataFrame:
    df = _frame(
        _data(payload),
        {
            "publishTime": "publish_time",
            "startTime": "start_time",
            "settlementDate": "settlement_date",
            "settlementPeriod": "settlement_period",
            "boundary": "boundary",
            "demand": "demand_mw",
        },
    )
    if df.empty:
        return df
    return df.assign(
        publish_time=_ts(df["publish_time"]),
        start_time=_ts(df["start_time"]),
        settlement_date=pd.to_datetime(df["settlement_date"]).dt.date,
        settlement_period=df["settlement_period"].astype("int16"),
        demand_mw=df["demand_mw"].astype("float64"),
    )


# --- WINDFOR: wind generation forecast ---------------------------------------


def fetch_windfor(start: date, end: date) -> Any:
    return http.get_json(
        f"{BASE}/datasets/WINDFOR",
        {
            "publishDateTimeFrom": _iso(start),
            "publishDateTimeTo": _iso(end + timedelta(days=1)),
            "format": "json",
        },
    )


def normalise_windfor(payload: Any) -> pd.DataFrame:
    df = _frame(
        _data(payload),
        {"publishTime": "publish_time", "startTime": "start_time", "generation": "generation_mw"},
    )
    if df.empty:
        return df
    return df.assign(
        publish_time=_ts(df["publish_time"]),
        start_time=_ts(df["start_time"]),
        generation_mw=df["generation_mw"].astype("float64"),
    )


DATASETS = [
    Dataset("elexon", "mid", 7, fetch_mid, normalise_mid, description="Market index price"),
    Dataset(
        "elexon",
        "demand_outturn",
        28,
        fetch_demand_outturn,
        normalise_demand_outturn,
        description="Initial demand outturn (INDO, ITSDO)",
    ),
    Dataset("elexon", "fuelhh", 7, fetch_fuelhh, normalise_fuelhh, description="Gen by fuel"),
    Dataset(
        "elexon",
        "ndf",
        7,
        fetch_ndf,
        normalise_ndf,
        description="National demand forecast vintages (publish-date chunks)",
    ),
    Dataset(
        "elexon",
        "windfor",
        7,
        fetch_windfor,
        normalise_windfor,
        description="Wind generation forecast vintages (publish-date chunks)",
    ),
]
