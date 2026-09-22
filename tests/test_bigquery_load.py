import pandas as pd
import yaml

from elecprice.bigquery_load import plan, to_bigquery_frame
from tests.conftest import FIXTURE_LAKE, REPO


def test_plan_targets_match_dbt_source_tables():
    tables = {p.table for p in plan(FIXTURE_LAKE)}
    sources = yaml.safe_load((REPO / "dbt" / "models" / "staging" / "_sources.yml").read_text())
    declared = {t["name"] for t in sources["sources"][0]["tables"]}
    assert tables == declared


def test_plan_counts_rows_without_credentials():
    plans = {p.table: p for p in plan(FIXTURE_LAKE)}
    assert plans["elexon_mid"].rows > 1000


def test_naive_timestamps_become_utc_aware_for_bigquery():
    df = pd.DataFrame({"t": pd.to_datetime(["2024-01-01 00:30"]), "x": [1]})
    out = to_bigquery_frame(df)
    assert str(out["t"].dt.tz) == "UTC"
    assert out["t"].iloc[0] == pd.Timestamp("2024-01-01 00:30", tz="UTC")


def test_bigquery_marts_schema_switch(monkeypatch):
    from elecprice.modelling.data import _marts_schema

    monkeypatch.delenv("ELEC_WAREHOUSE", raising=False)
    assert _marts_schema() == "marts"
    monkeypatch.setenv("ELEC_WAREHOUSE", "bigquery")
    monkeypatch.setenv("GCP_PROJECT", "p")
    monkeypatch.setenv("BQ_DATASET", "elecprice")
    assert _marts_schema() == "`p`.`elecprice_marts`"
