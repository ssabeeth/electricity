"""Command-line entry point: ``elec <command>``.

Every pipeline step is reachable from here so that Airflow, Make and humans all
run exactly the same code path.
"""

from __future__ import annotations

import argparse
import sys

from elecprice import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="elec", description=__doc__)
    parser.add_argument("--version", action="version", version=f"elecprice {__version__}")
    parser.add_subparsers(dest="command", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 1
    return int(handler(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
