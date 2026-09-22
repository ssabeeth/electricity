"""Command-line entry point: ``elec <command>``.

Every pipeline step is reachable from here so that Airflow, Make and humans all
run exactly the same code path.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

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
    if args.days is not None:
        start = date.today() - timedelta(days=args.days)
    else:
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


def cmd_backtest(args: argparse.Namespace) -> int:
    from elecprice.modelling.data import ModelConfig
    from elecprice.modelling.runner import backtest

    overrides = {}
    if args.target_mode:
        overrides["target_mode"] = args.target_mode
    if args.calibration:
        overrides["calibration"] = {"mode": args.calibration, "days": 56}
    config = ModelConfig.load(**overrides)
    backtest(config, use_mlflow=not args.no_mlflow, write_report=not args.no_report)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from elecprice.config import REPO_ROOT
    from elecprice.modelling import report
    from elecprice.modelling.runner import backtest_dir

    report.write_figures(backtest_dir(), REPO_ROOT / "reports" / "figures")
    report.write_markdown(backtest_dir(), REPO_ROOT / "reports" / "backtest.md")
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    from elecprice.modelling.runner import fit_final, register_model, train_candidate

    res = train_candidate(eval_days=args.eval_days)
    model = fit_final(res["config"], res["frame"])
    metrics = {f"latest_fold_{k}": v for k, v in res["candidate_metrics"].items() if k != "n"}
    tags = {
        "eval_fold": f"{res['fold'][0]}..{res['fold'][1]}",
        "trained_through": str(res["frame"]["settlement_date"].max().date()),
    }
    sample = res["frame"].drop(columns=["price_gbp_mwh"]).tail(48)
    version = register_model(model, res["config"], metrics, tags, alias=args.alias, sample=sample)
    print(f"registered version {version} (alias={args.alias}) {metrics}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from elecprice.battery import report
    from elecprice.battery.simulate import battery_dir, run
    from elecprice.config import REPO_ROOT

    summary = run(workers=args.workers)
    report.write_report(
        battery_dir(), REPO_ROOT / "reports" / "battery.md", REPO_ROOT / "reports" / "figures"
    )
    if not args.no_mlflow:
        import mlflow

        from elecprice.modelling import tracking

        tracking.configure("elecprice-battery")
        with mlflow.start_run(run_name="battery-simulation"):
            mlflow.log_params(json.loads((battery_dir() / "params.json").read_text()))
            for _, r in summary.iterrows():
                mlflow.log_metrics(
                    {
                        f"{r['strategy']}_net_gbp": float(r["net_gbp"]),
                        f"{r['strategy']}_net_gbp_per_mw_year": float(r["net_gbp_per_mw_year"]),
                        f"{r['strategy']}_capture_vs_perfect": float(r["capture_vs_perfect"]),
                    }
                )
            mlflow.log_artifact(str(REPO_ROOT / "reports" / "battery.md"))
            mlflow.log_artifact(str(battery_dir() / "summary.csv"))
    print(summary.round(3).to_string(index=False))
    return 0


def cmd_forecast(args: argparse.Namespace) -> int:
    from elecprice.pipeline.live import forecast_day

    out = forecast_day(args.date)
    lg = out[out["model"] == "lgbm_quantile"]
    print(lg[["settlement_period", "p10", "p50", "p90"]].round(1).to_string(index=False))
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from elecprice.pipeline.live import schedule_day

    schedule_day(args.date)
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    from elecprice.pipeline.live import monitor

    print(monitor())
    return 0


def cmd_retrain(args: argparse.Namespace) -> int:
    from elecprice.pipeline.retrain import retrain

    result = retrain(min_eval_days=args.min_eval_days)
    print(json.dumps(result, indent=2, default=str))
    return 0


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
    p.add_argument("--days", type=int, help="ingest only the last N days (overrides --start)")
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

    p = sub.add_parser("backtest", help="Walk-forward backtest, MLflow logging and report")
    p.add_argument("--target-mode", choices=["level", "delta_7d_mean"])
    p.add_argument("--calibration", choices=["none", "outer", "all"])
    p.add_argument("--no-mlflow", action="store_true")
    p.add_argument("--no-report", action="store_true")
    p.set_defaults(handler=cmd_backtest)

    p = sub.add_parser("report", help="Rebuild reports/backtest.md from saved backtest outputs")
    p.set_defaults(handler=cmd_report)

    p = sub.add_parser("train", help="Train on all data and register the model in MLflow")
    p.add_argument("--eval-days", type=int, default=28, help="latest fold length for metrics")
    p.add_argument("--alias", default=None, help="registry alias to set, e.g. champion")
    p.set_defaults(handler=cmd_train)

    p = sub.add_parser("simulate", help="Battery arbitrage simulation over backtest forecasts")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--no-mlflow", action="store_true")
    p.set_defaults(handler=cmd_simulate)

    p = sub.add_parser("forecast", help="Forecast a delivery day with the champion model")
    p.add_argument("--date", type=_date, help="delivery date (default: tomorrow, UK)")
    p.set_defaults(handler=cmd_forecast)

    p = sub.add_parser("schedule", help="Battery schedule for a delivery day from its forecast")
    p.add_argument("--date", type=_date, help="delivery date (default: tomorrow, UK)")
    p.set_defaults(handler=cmd_schedule)

    p = sub.add_parser("monitor", help="Settle live forecasts and schedules against actuals")
    p.set_defaults(handler=cmd_monitor)

    p = sub.add_parser("retrain", help="Weekly champion/challenger retrain and promotion")
    p.add_argument("--min-eval-days", type=int, default=5)
    p.set_defaults(handler=cmd_retrain)

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
