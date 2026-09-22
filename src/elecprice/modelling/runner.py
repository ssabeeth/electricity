"""High-level modelling entry points used by the CLI and Airflow."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import mlflow
import pandas as pd
from mlflow.tracking import MlflowClient

from elecprice.config import REPO_ROOT, get_settings
from elecprice.logging_utils import get_logger
from elecprice.modelling import report, tracking
from elecprice.modelling.backtest import BacktestResult, run_backtest
from elecprice.modelling.data import TARGET, ModelConfig, load_frame
from elecprice.modelling.metrics import evaluate
from elecprice.modelling.models import QuantileLGBM, SeasonalNaive

log = get_logger(__name__)

BACKTEST_EXPERIMENT = "elecprice-backtest"
TRAINING_EXPERIMENT = "elecprice-training"


def backtest_dir() -> Path:
    return get_settings().outputs_dir / "backtest"


def save_backtest(result: BacktestResult, out_dir: Path, run_id: str | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    result.predictions.to_parquet(out_dir / "predictions.parquet", index=False)
    result.fold_metrics.to_csv(out_dir / "fold_metrics.csv", index=False)
    result.summary().to_csv(out_dir / "summary.csv", index=False)
    result.importance.rename("gain_share").to_csv(out_dir / "importance.csv")
    folds = result.folds
    meta = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "mlflow_run_id": run_id,
        "n_folds": len(folds),
        "first_test_month": folds[0].label,
        "last_test_month": folds[-1].label,
        "selection_folds": result.config.backtest.get("selection_folds", 0),
        "config": result.config.to_flat_dict(),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))


def backtest(
    config: ModelConfig | None = None,
    *,
    use_mlflow: bool = True,
    write_report: bool = True,
) -> BacktestResult:
    config = config or ModelConfig.load()
    df = load_frame()
    result = run_backtest(df, config)
    summary = result.summary()
    out_dir = backtest_dir()
    run_id = None

    if use_mlflow:
        tracking.configure(BACKTEST_EXPERIMENT)
        with mlflow.start_run(run_name=f"backtest-{datetime.now(UTC):%Y%m%d-%H%M}") as parent:
            run_id = parent.info.run_id
            mlflow.log_params(config.to_flat_dict())
            mlflow.set_tags({"n_folds": len(result.folds), "target": TARGET})
            for model, g in result.fold_metrics.groupby("model"):
                with mlflow.start_run(run_name=model, nested=True):
                    mlflow.set_tag("model", model)
                    for _, row in g.iterrows():
                        mlflow.log_metrics(
                            {
                                k: float(row[k])
                                for k in ("pinball_mean", "mae_p50", "rmse_p50", "coverage")
                            },
                            step=int(row["fold"]),
                        )
                    for _, row in summary[summary["model"] == model].iterrows():
                        mlflow.log_metrics(
                            {
                                f"{row['scope']}_{k}": float(row[k])
                                for k in (
                                    "pinball_mean",
                                    "mae_p50",
                                    "rmse_p50",
                                    "coverage",
                                    "interval_width",
                                    "pinball_skill_vs_baseline",
                                )
                            }
                        )
            save_backtest(result, out_dir, run_id)
            if write_report:
                _write_report(out_dir)
                mlflow.log_artifacts(str(REPO_ROOT / "reports" / "figures"), "figures")
                mlflow.log_artifact(str(REPO_ROOT / "reports" / "backtest.md"))
            for name in ("fold_metrics.csv", "summary.csv", "importance.csv", "meta.json"):
                mlflow.log_artifact(str(out_dir / name), "outputs")
    else:
        save_backtest(result, out_dir, run_id)
        if write_report:
            _write_report(out_dir)

    log.info("backtest summary:\n%s", summary.round(3).to_string(index=False))
    return result


def _write_report(out_dir: Path) -> None:
    report.write_figures(out_dir, REPO_ROOT / "reports" / "figures")
    report.write_markdown(out_dir, REPO_ROOT / "reports" / "backtest.md")


def latest_fold_window(df: pd.DataFrame, days: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    known = df[df[TARGET].notna()]
    end = known["settlement_date"].max()
    return end - pd.Timedelta(days=days - 1), end


def train_candidate(
    config: ModelConfig | None = None,
    *,
    eval_days: int = 28,
    register: bool = True,
) -> dict:
    """Fit a candidate on data before the latest ``eval_days`` fold and score it there.

    Returns metrics for the candidate and the baseline on that fold, the run id
    and (if registered) the new model version. The refit on all data happens
    in ``promote_if_better`` only if the candidate wins.
    """
    config = config or ModelConfig.load()
    df = load_frame()
    start, end = latest_fold_window(df, eval_days)
    gap = config.backtest["gap_days"]
    train = df[df["settlement_date"] <= start - pd.Timedelta(days=gap)]
    test = df[(df["settlement_date"] >= start) & (df["settlement_date"] <= end)]

    candidate = QuantileLGBM(
        config.quantiles,
        config.lightgbm,
        config.target_mode,
        calibrate=config.calibration.get("mode", "none"),
        calibration_days=config.calibration.get("days", 56),
        gap_days=gap,
    ).fit(train)
    baseline = SeasonalNaive(config.quantiles, **config.baseline).fit(train)
    cand_m = evaluate(test[TARGET], candidate.predict(test), config.quantiles)
    base_m = evaluate(test[TARGET], baseline.predict(test), config.quantiles)
    return {
        "config": config,
        "candidate": candidate,
        "candidate_metrics": cand_m,
        "baseline_metrics": base_m,
        "fold": (start.date(), end.date()),
        "train_end": (start - pd.Timedelta(days=gap)).date(),
        "frame": df,
        "test": test,
    }


def fit_final(config: ModelConfig, df: pd.DataFrame) -> QuantileLGBM:
    return QuantileLGBM(
        config.quantiles,
        config.lightgbm,
        config.target_mode,
        calibrate=config.calibration.get("mode", "none"),
        calibration_days=config.calibration.get("days", 56),
        gap_days=config.backtest["gap_days"],
    ).fit(df)


def register_model(
    model: QuantileLGBM,
    config: ModelConfig,
    metrics: dict[str, float],
    tags: dict[str, str],
    *,
    alias: str | None = None,
) -> str:
    """Log ``model`` in a training run and register it. Returns the version."""
    tracking.configure(TRAINING_EXPERIMENT)
    with mlflow.start_run(run_name=f"train-{datetime.now(UTC):%Y%m%d-%H%M}"):
        mlflow.log_params(config.to_flat_dict())
        mlflow.log_metrics({k: float(v) for k, v in metrics.items()})
        mlflow.set_tags(tags)
        uri = tracking.log_quantile_model(model)
    mv = mlflow.register_model(uri, tracking.REGISTERED_MODEL, tags=tags)
    client = MlflowClient()
    for k, v in metrics.items():
        client.set_model_version_tag(tracking.REGISTERED_MODEL, mv.version, k, f"{v:.4f}")
    if alias:
        client.set_registered_model_alias(tracking.REGISTERED_MODEL, alias, mv.version)
    return mv.version
