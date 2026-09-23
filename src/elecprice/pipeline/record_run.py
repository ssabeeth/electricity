"""The daily job behind the public track record (``elec track-record daily``).

record -> Parquet outputs -> (monthly refit) -> forecast and schedule tomorrow,
once -> settle past days -> scores, model history and README back into the record.
Ingestion and ``dbt build`` run before this, as in the Airflow DAG, so the
point-in-time test gates every forecast.

**Monthly refit.** The first forecast of each calendar month is made by a model
refitted on every delivery day up to two days before it: at the 09:00 cutoff on
the day before, that is the newest day whose prices are fully known. This is the
walk-forward backtest's protocol (monthly folds, expanding window, fixed
configuration), so the live record measures the same method the backtest did.
Each refit is exported with file hashes and published as a release, and every
forecast file names the model that made it.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from elecprice.config import get_settings
from elecprice.logging_utils import get_logger
from elecprice.modelling.data import ModelConfig, load_frame
from elecprice.modelling.models import EXPORT_META, export_model
from elecprice.pipeline import track_record
from elecprice.pipeline.live import forecast_day, monitor, schedule_day, tomorrow_uk
from elecprice.timeutils import decision_cutoff_utc

log = get_logger(__name__)

MODELS_FILE = "models.json"
# The record's first model, used by `plan` before models.json exists.
FIRST_MODEL = {"release": "model-v1", "month": "2026-09"}
MODEL_FIELDS = (
    "release",
    "month",
    "trained_from",
    "trained_through",
    "training_rows",
    "exported_at",
    "sha256",
)


def model_history(record: Path) -> list[dict]:
    path = record / MODELS_FILE
    return json.loads(path.read_text()) if path.exists() else []


def _month(meta: dict) -> str:
    """The first month a model serves (older exports only carry exported_at)."""
    return meta.get("month") or meta["exported_at"][:7]


def needs_refit(day: date, current: dict) -> bool:
    """True for the first delivery day of a month the current model was not made for."""
    return day.strftime("%Y-%m") > _month(current)


def plan(record: Path, now: pd.Timestamp | None = None) -> dict:
    """What today's run will do, for the workflow: which release to fetch, whether to refit."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    day = tomorrow_uk(now.to_pydatetime())
    history = model_history(record)
    current = history[-1] if history else FIRST_MODEL
    return {
        "delivery_date": str(day),
        "release": current["release"],
        "refit": needs_refit(day, current),
    }


def _feature_start() -> pd.Timestamp:
    project = yaml.safe_load((get_settings().dbt_project_dir / "dbt_project.yml").read_text())
    return pd.Timestamp(project["vars"]["feature_start_date"])


def refit(day: date, out_dir: Path, config: ModelConfig | None = None) -> dict:
    """Fit the model that serves ``day``'s month, on every delivery day up to ``day - gap``."""
    from elecprice.modelling.backtest import assert_no_leakage
    from elecprice.modelling.runner import fit_final

    config = config or ModelConfig.load()
    train_end = pd.Timestamp(day) - pd.Timedelta(days=config.backtest["gap_days"])
    frame = load_frame(end=str(day))
    train = frame[frame["settlement_date"] <= train_end]
    target_day = frame[frame["settlement_date"] == pd.Timestamp(day)]
    if train.empty or target_day.empty:
        raise RuntimeError(f"cannot refit for {day}: no training rows or no rows for the day")
    if train["settlement_date"].min() > _feature_start() + pd.Timedelta(days=7):
        raise RuntimeError(
            f"refit needs the full history from {_feature_start().date()}, but the warehouse "
            f"starts at {train['settlement_date'].min().date()}; ingest from ELEC_HISTORY_START"
        )
    assert_no_leakage(train, target_day)
    model = fit_final(config, train)
    meta = export_model(
        model,
        out_dir,
        release=f"model-{day:%Y-%m}",
        month=day.strftime("%Y-%m"),
        trained_from=str(train["settlement_date"].min().date()),
        trained_through=str(train["settlement_date"].max().date()),
        training_rows=len(train),
        method="monthly refit on every delivery day up to D-2, configs/model.yaml",
    )
    log.info(
        "refitted %s on %s..%s", meta["release"], meta["trained_from"], meta["trained_through"]
    )
    return meta


def _summary(meta: dict) -> dict:
    return {k: meta[k] for k in MODEL_FIELDS if k in meta}


def run(
    record: Path,
    repo_url: str,
    delivery_date: date | None = None,
    now: pd.Timestamp | None = None,
    model_dir: Path | None = None,
) -> dict:
    settings = get_settings()
    outputs = settings.outputs_dir
    track_record.materialise(record, outputs)
    if model_dir is None and os.environ.get("ELEC_MODEL_DIR"):
        model_dir = Path(os.environ["ELEC_MODEL_DIR"])

    history = model_history(record)
    if model_dir and not history:  # the record's first model
        history = [_summary(json.loads((model_dir / EXPORT_META).read_text()))]

    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    day = delivery_date or tomorrow_uk(now.to_pydatetime())
    cutoff = decision_cutoff_utc(day, settings.cutoff_local)
    recorded, new_release = False, None
    if track_record.has_day(record, day):
        log.info("%s is already in the record; published forecasts are never replaced", day)
    elif now < cutoff:
        # An early run (a manual one, say) settles past days and waits for the cutoff.
        log.info("the cutoff for %s is %s UTC and has not passed; not forecasting", day, cutoff)
    else:
        if model_dir and needs_refit(day, history[-1]):
            model_dir = settings.data_dir / "models" / f"model-{day:%Y-%m}"
            meta = refit(day, model_dir)
            history.append(_summary(meta))
            new_release = meta["release"]
        forecasts = forecast_day(day, model_dir=model_dir)
        schedules = schedule_day(day)
        recorded = track_record.record_day(record, day, forecasts, schedules)

    settled = monitor()
    track_record.write_scores(record, outputs)
    if history:
        (record / MODELS_FILE).write_text(json.dumps(history, indent=2) + "\n")
    track_record.write_readme(record, repo_url, history)
    return {
        "delivery_date": str(day),
        "recorded": recorded,
        "new_release": new_release,
        "model_dir": str(model_dir) if model_dir else None,
        **settled,
    }


__all__ = ["model_history", "needs_refit", "plan", "refit", "run"]
