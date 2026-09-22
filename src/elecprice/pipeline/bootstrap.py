"""One-shot, idempotent bootstrap from a clean clone.

ingest history -> dbt build -> backtest (if missing) -> champion (if missing)
-> battery simulation (if missing) -> forecast + schedule for the next delivery
day whose cutoff has passed. Re-running skips everything already done, and
ingestion reuses the raw cache, so a restart never re-downloads history.
"""

from __future__ import annotations

from datetime import timedelta

from mlflow.exceptions import MlflowException

from elecprice.config import get_settings
from elecprice.dbt_runner import run_dbt
from elecprice.ingest import ingest
from elecprice.logging_utils import get_logger

log = get_logger(__name__)


def has_champion() -> bool:
    from elecprice.modelling import tracking
    from elecprice.modelling.runner import TRAINING_EXPERIMENT

    tracking.configure(TRAINING_EXPERIMENT)
    try:
        from mlflow.tracking import MlflowClient

        MlflowClient().get_model_version_by_alias(tracking.REGISTERED_MODEL, tracking.CHAMPION)
        return True
    except MlflowException:
        return False


def train_champion_if_missing() -> bool:
    if has_champion():
        log.info("champion already registered; skipping training")
        return False
    from elecprice.modelling.runner import fit_final, register_model, train_candidate

    res = train_candidate()
    model = fit_final(res["config"], res["frame"])
    metrics = {f"latest_fold_{k}": v for k, v in res["candidate_metrics"].items() if k != "n"}
    tags = {"trained_through": str(res["frame"]["settlement_date"].max().date())}
    sample = res["frame"].drop(columns=["price_gbp_mwh"]).tail(48)
    register_model(model, res["config"], metrics, tags, alias="champion", sample=sample)
    return True


def forecast_next_available() -> None:
    """Forecast tomorrow if its cutoff has passed, otherwise today."""
    from elecprice.pipeline.live import forecast_day, monitor, schedule_day, tomorrow_uk

    for d in (tomorrow_uk(), tomorrow_uk() - timedelta(days=1)):
        try:
            forecast_day(d)
            schedule_day(d)
            break
        except RuntimeError as exc:
            log.info("no forecast for %s yet: %s", d, exc)
    monitor()


def bootstrap() -> None:
    settings = get_settings()
    reports = ingest(None, settings.history_start)
    failed = [r.dataset for r in reports if not r.ok]
    if failed:
        log.warning("some ingestion chunks failed (will retry next run): %s", failed)
    if run_dbt(["build"]) != 0:
        raise RuntimeError("dbt build failed; see dbt logs")
    out = settings.outputs_dir
    if not (out / "backtest" / "summary.csv").exists():
        from elecprice.modelling.runner import backtest

        backtest()
    train_champion_if_missing()
    if not (out / "battery" / "summary.csv").exists():
        from elecprice.battery.simulate import run

        run()
    forecast_next_available()
    log.info("bootstrap complete")
