"""Cut a small, committed sample of the Parquet lake for CI and tests.

Window: 2024-03-01 .. 2024-04-07 (includes the 2024-03-31 clock change).
Forecast vintages are thinned to those issued 05:00-10:59 UTC, which still
straddles the 09:00 UK cutoff so the point-in-time tests have work to do.

    uv run python scripts/make_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
LAKE = REPO / "data" / "lake"
OUT = REPO / "tests" / "fixtures" / "lake"
START, END = "2024-03-01", "2024-04-07"

QUERIES = {
    "elexon/mid": f"settlement_date between '{START}' and '{END}'",
    "elexon/demand_outturn": f"settlement_date between '{START}' and '{END}'",
    "elexon/fuelhh": f"settlement_date between '{START}' and '{END}'",
    "elexon/ndf": f"settlement_date between '{START}' and '{END}' "
    "and hour(publish_time) between 5 and 10",
    "elexon/windfor": f"start_time between '{START}' and '{END} 23:00' "
    "and hour(publish_time) between 5 and 10",
    "neso/embedded_forecast": f"settlement_date between '{START}' and '{END}' "
    "and hour(forecast_issued_at) between 5 and 10",
    "openmeteo/weather_forecast": f"valid_time between '{START}' and '{END} 23:00'",
    "carbon/intensity": f"start_time between '{START}' and '{END} 23:30'",
}


def main() -> None:
    con = duckdb.connect()
    for ds, where in QUERIES.items():
        out = OUT / ds / "fixture.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        con.execute(
            f"copy (select * from read_parquet('{LAKE / ds}/*.parquet') where {where} "
            f"order by all) to '{out}' (format parquet, compression zstd)"
        )
        n = con.execute(f"select count(*) from '{out}'").fetchone()[0]
        print(f"{ds:30s} {n:>8,d} rows  {out.stat().st_size / 1024:8.0f} KiB")


if __name__ == "__main__":
    main()
