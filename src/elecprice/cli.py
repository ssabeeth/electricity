"""Command-line entry point: ``elec <command>``.

Every pipeline step is reachable from here so that Airflow, Make and humans all
run exactly the same code path.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from elecprice import __version__
from elecprice.config import get_settings


def _date(s: str) -> date:
    return date.fromisoformat(s)


def cmd_ingest(args: argparse.Namespace) -> int:
    from elecprice.ingest import REGISTRY, ingest

    if args.list:
        for key, ds in REGISTRY.items():
            print(f"{key:30s} chunk={ds.chunk_days:>2}d  {ds.description}")
        return 0
    start = args.start or get_settings().history_start
    reports = ingest(
        args.datasets, start, args.end, force=args.force, refresh_days=args.refresh_days
    )
    for r in reports:
        print(r)
    return 0 if all(r.ok for r in reports) else 1


def cmd_dbt(args: argparse.Namespace) -> int:
    from elecprice.dbt_runner import run_dbt

    return run_dbt(args.dbt_args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="elec", description=__doc__)
    parser.add_argument("--version", action="version", version=f"elecprice {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="Pull sources into the raw cache and Parquet lake")
    p.add_argument(
        "datasets",
        nargs="*",
        help="source or source/name (default: all). e.g. elexon, elexon/mid",
    )
    p.add_argument("--start", type=_date, help="first date (default ELEC_HISTORY_START)")
    p.add_argument("--end", type=_date, help="last date (default today + look-ahead)")
    p.add_argument("--force", action="store_true", help="re-fetch even settled chunks")
    p.add_argument(
        "--refresh-days",
        type=int,
        default=3,
        help="chunks ending within this many days of today are always re-fetched",
    )
    p.add_argument("--list", action="store_true", help="list datasets and exit")
    p.set_defaults(handler=cmd_ingest)

    p = sub.add_parser("dbt", help="Run dbt with project paths wired in (e.g. elec dbt build)")
    p.add_argument("dbt_args", nargs=argparse.REMAINDER, help="arguments passed to dbt")
    p.set_defaults(handler=cmd_dbt)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
