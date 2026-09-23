"""The daily job behind the public track record (``elec track-record daily``).

record -> Parquet outputs -> forecast and schedule tomorrow (once) -> settle past
days -> scores and README back into the record. Ingestion and ``dbt build`` run
before this, as in the Airflow DAG, so the point-in-time test gates every forecast.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pandas as pd

from elecprice.config import get_settings
from elecprice.logging_utils import get_logger
from elecprice.pipeline import track_record
from elecprice.pipeline.live import EXPORT_META, forecast_day, monitor, schedule_day, tomorrow_uk
from elecprice.timeutils import decision_cutoff_utc

log = get_logger(__name__)


def run(
    record: Path,
    repo_url: str,
    delivery_date: date | None = None,
    now: pd.Timestamp | None = None,
) -> dict:
    settings = get_settings()
    outputs = settings.outputs_dir
    track_record.materialise(record, outputs)

    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    day = delivery_date or tomorrow_uk(now.to_pydatetime())
    cutoff = decision_cutoff_utc(day, settings.cutoff_local)
    recorded = False
    if track_record.has_day(record, day):
        log.info("%s is already in the record; published forecasts are never replaced", day)
    elif now < cutoff:
        # An early run (a manual one, say) settles past days and waits for the cutoff.
        log.info("the cutoff for %s is %s UTC and has not passed; not forecasting", day, cutoff)
    else:
        forecasts = forecast_day(day)
        schedules = schedule_day(day)
        recorded = track_record.record_day(record, day, forecasts, schedules)

    settled = monitor()
    track_record.write_scores(record, outputs)
    model = _model_meta()
    if model:
        (record / "model.json").write_text(json.dumps(model, indent=2) + "\n")
    track_record.write_readme(record, repo_url, model)
    return {"delivery_date": str(day), "recorded": recorded, **settled}


MODEL_FIELDS = ("version", "trained_through", "exported_at", "sha256")


def _model_meta() -> dict:
    model_dir = os.environ.get("ELEC_MODEL_DIR")
    if not model_dir:
        return {}
    meta = json.loads((Path(model_dir) / EXPORT_META).read_text())
    return {k: meta[k] for k in MODEL_FIELDS if k in meta}


__all__ = ["run"]
