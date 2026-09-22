from datetime import date, time

import pandas as pd
import pytest

from elecprice.timeutils import (
    date_chunks,
    decision_cutoff_utc,
    periods_in_day,
    settlement_calendar,
    settlement_periods,
    to_settlement,
)


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (date(2024, 3, 30), 48),
        (date(2024, 3, 31), 46),  # clocks go forward
        (date(2024, 10, 27), 50),  # clocks go back
        (date(2024, 7, 1), 48),
    ],
)
def test_periods_in_day_handles_dst(d, expected):
    assert periods_in_day(d) == expected


def test_settlement_period_one_starts_at_local_midnight():
    summer = settlement_periods(date(2024, 7, 1))
    assert summer.iloc[0]["start_time_utc"] == pd.Timestamp("2024-06-30 23:00", tz="UTC")
    winter = settlement_periods(date(2024, 1, 15))
    assert winter.iloc[0]["start_time_utc"] == pd.Timestamp("2024-01-15 00:00", tz="UTC")


def test_cutoff_is_nine_am_uk_on_previous_day():
    # BST: 09:00 local == 08:00 UTC
    assert decision_cutoff_utc(date(2024, 7, 2)) == pd.Timestamp("2024-07-01 08:00", tz="UTC")
    # GMT: 09:00 local == 09:00 UTC
    assert decision_cutoff_utc(date(2024, 1, 16)) == pd.Timestamp("2024-01-15 09:00", tz="UTC")
    # custom cutoff
    assert decision_cutoff_utc(date(2024, 1, 16), time(11, 30)) == pd.Timestamp(
        "2024-01-15 11:30", tz="UTC"
    )


def test_to_settlement_round_trips_calendar_including_dst_days():
    cal = settlement_calendar(date(2024, 3, 29), date(2024, 4, 2))
    cal = pd.concat([cal, settlement_calendar(date(2024, 10, 26), date(2024, 10, 28))])
    back = to_settlement(cal["start_time_utc"])
    assert list(back["settlement_date"]) == list(cal["settlement_date"])
    assert list(back["settlement_period"]) == list(cal["settlement_period"])


def test_date_chunks_cover_range_without_overlap():
    chunks = date_chunks(date(2024, 1, 1), date(2024, 1, 20), 7)
    assert chunks[0] == (date(2024, 1, 1), date(2024, 1, 7))
    assert chunks[-1] == (date(2024, 1, 15), date(2024, 1, 20))
    assert sum((b - a).days + 1 for a, b in chunks) == 20
