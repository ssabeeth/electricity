"""Time conventions used everywhere in the project.

* All stored timestamps are UTC.
* A *delivery day* (``settlement_date``) is a UK local calendar day. Settlement
  period 1 starts at 00:00 Europe/London, so a day has 46, 48 or 50 periods
  depending on daylight-saving changes.
* The *decision cutoff* for delivery day D is ``cutoff_local`` (09:00 by
  default) Europe/London on D-1. Anything used to forecast D must have been
  available at or before that instant.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

from elecprice.config import UK_TZ

HALF_HOUR = pd.Timedelta(minutes=30)


def local_midnight_utc(d: date) -> pd.Timestamp:
    """UTC instant of 00:00 Europe/London on ``d``."""
    return pd.Timestamp(datetime.combine(d, time(0))).tz_localize(UK_TZ).tz_convert("UTC")


def periods_in_day(d: date) -> int:
    start = local_midnight_utc(d)
    end = local_midnight_utc(d + timedelta(days=1))
    return int((end - start) / HALF_HOUR)


def settlement_periods(d: date) -> pd.DataFrame:
    """All settlement periods of delivery day ``d`` with their UTC start times."""
    start = local_midnight_utc(d)
    n = periods_in_day(d)
    starts = pd.date_range(start, periods=n, freq="30min")
    return pd.DataFrame(
        {
            "settlement_date": pd.Timestamp(d).date(),
            "settlement_period": range(1, n + 1),
            "start_time_utc": starts,
        }
    )


def settlement_calendar(start: date, end: date) -> pd.DataFrame:
    """Settlement periods for every delivery day in ``[start, end]`` inclusive."""
    days = pd.date_range(start, end, freq="D")
    return pd.concat([settlement_periods(d.date()) for d in days], ignore_index=True)


def decision_cutoff_utc(delivery_date: date, cutoff_local: time = time(9, 0)) -> pd.Timestamp:
    """The instant (UTC) at which the forecast for ``delivery_date`` is frozen."""
    local = datetime.combine(delivery_date - timedelta(days=1), cutoff_local)
    return pd.Timestamp(local).tz_localize(UK_TZ).tz_convert("UTC")


def to_settlement(ts_utc: pd.Series) -> pd.DataFrame:
    """Map UTC half-hour start times to (settlement_date, settlement_period)."""
    ts = pd.to_datetime(ts_utc, utc=True)
    local = ts.dt.tz_convert(UK_TZ)
    sdate = local.dt.date
    midnight = pd.to_datetime(sdate).dt.tz_localize(UK_TZ).dt.tz_convert("UTC")
    sp = ((ts - midnight) / HALF_HOUR).astype(int) + 1
    return pd.DataFrame({"settlement_date": sdate, "settlement_period": sp})


def date_chunks(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """Split ``[start, end]`` (inclusive) into consecutive chunks of at most ``days`` days."""
    out = []
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out
