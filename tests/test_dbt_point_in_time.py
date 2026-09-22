"""dbt-level guarantees, run against the committed fixture lake.

* the full project builds and every test passes (including the point-in-time
  tests and the as-of unit tests);
* a deliberately leaky model is caught by the point-in-time test.
"""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from tests.conftest import run_fixture_dbt

pytestmark = pytest.mark.slow


def test_feature_mart_builds_and_respects_cutoffs(fixture_warehouse):
    with duckdb.connect(str(fixture_warehouse), read_only=True) as con:
        mart = con.sql("select * from marts.mart_features").df()
    assert len(mart) > 30 * 46
    # Every availability timestamp is at or before the row's cutoff.
    ts_cols = [
        c for c in mart.columns if c.endswith(("_published_at", "_issued_at", "_available_at"))
    ]
    assert len(ts_cols) == 6
    for col in ts_cols:
        known = mart[col].notna()
        assert (mart.loc[known, col] <= mart.loc[known, "cutoff_utc"]).all(), col
    # The clock-change day has 46 periods and they are all present.
    day = mart[mart["settlement_date"] == pd.Timestamp("2024-03-31")]
    assert len(day) == 46
    assert set(day["periods_in_day"]) == {46}


def test_features_are_well_populated_on_fixtures(fixture_warehouse):
    with duckdb.connect(str(fixture_warehouse), read_only=True) as con:
        null_share = con.sql(
            """
            select
                avg((ndf_demand_mw is null)::int) as ndf,
                avg((windfor_mw is null)::int) as windfor,
                avg((emb_solar_mw is null)::int) as emb,
                avg((wx_temperature_c is null)::int) as wx,
                avg((price_d7_same_period is null)::int) as price
            from marts.mart_features
            """
        ).df()
    assert (null_share.iloc[0] < 0.02).all(), null_share


def test_point_in_time_test_catches_leaky_model(fixture_warehouse, tmp_path):
    result = run_fixture_dbt(
        ["build", "--select", "pit_canary_leaky"],
        fixture_warehouse,
        tmp_path,
        extra_vars={"pit_canary": True},
    )
    assert result.returncode != 0, "the leaky canary should fail its point_in_time test"
    assert "FAIL" in result.stdout and "pit_canary_leaky" in result.stdout
