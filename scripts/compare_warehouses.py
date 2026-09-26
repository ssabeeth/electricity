"""Compare the feature mart built on BigQuery with the local DuckDB build.

Reads ``mart_features`` joined to ``fct_price_actuals`` from both warehouses
(``load_frame``), keeps the delivery days both cover, and checks every value.
The settlement calendar runs two days past the build date, so a later build has
a few extra, empty days; they are counted and dropped, not compared. Prints
counts only.

    GCP_PROJECT=... uv run python scripts/compare_warehouses.py
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from elecprice.modelling.data import load_frame

KEY = ["settlement_date", "settlement_period"]


def _load(warehouse: str) -> pd.DataFrame:
    os.environ["ELEC_WAREHOUSE"] = warehouse
    return load_frame().sort_values(KEY).reset_index(drop=True)


def main() -> None:
    duck, bq = _load("duckdb"), _load("bigquery")
    last = min(duck["settlement_date"].max(), bq["settlement_date"].max())
    extra = {
        name: df[df["settlement_date"] > last] for name, df in (("duckdb", duck), ("bigquery", bq))
    }
    for name, df in extra.items():
        priced = int(df["price_gbp_mwh"].notna().sum())
        print(f"{name}: {len(df)} rows after {last.date()}, {priced} with a price")
    duck = duck[duck["settlement_date"] <= last].reset_index(drop=True)
    bq = bq[bq["settlement_date"] <= last].reset_index(drop=True)
    assert duck[KEY].equals(bq[KEY]), "different delivery periods"
    assert list(duck.columns) == list(bq.columns), "different columns"
    mismatched = {}
    for c in duck.columns:
        a, b = duck[c], bq[c]
        if pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(b):
            a, b = a.to_numpy(float), b.to_numpy(float)
            same = (np.isnan(a) & np.isnan(b)) | np.isclose(a, b, rtol=1e-9, atol=1e-9)
        else:
            same = ((a.astype(str) == b.astype(str)) | (a.isna() & b.isna())).to_numpy()
        if not same.all():
            mismatched[c] = int((~same).sum())
    print(
        f"{len(duck):,} delivery periods x {duck.shape[1]} columns = {duck.size:,} values "
        f"compared through {last.date()}; mismatched columns: {mismatched or 'none'}"
    )
    if mismatched:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
