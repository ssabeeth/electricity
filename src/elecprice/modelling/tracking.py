"""MLflow setup and the registered-model wrapper.

Locally MLflow uses sqlite under ``$ELEC_DATA_DIR/mlflow`` with a fixed artifact
root; in Docker Compose ``MLFLOW_TRACKING_URI`` points at the MLflow server,
which stores artifacts itself.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

import mlflow
import pandas as pd
from mlflow.tracking import MlflowClient

from elecprice.config import get_settings
from elecprice.modelling.data import add_derived_features
from elecprice.modelling.models import QuantileLGBM, load_model, save_model

REGISTERED_MODEL = "elecprice-lgbm-quantile"
CHAMPION = "champion"


def configure(experiment: str) -> str:
    settings = get_settings()
    uri = settings.tracking_uri
    if uri.startswith("sqlite:///"):
        Path(uri.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(uri)
    exp = mlflow.get_experiment_by_name(experiment)
    if exp is None:
        artifact_location = None
        if uri.startswith(("sqlite:", "file:")):
            root = settings.data_dir / "mlflow" / "artifacts" / experiment
            root.mkdir(parents=True, exist_ok=True)
            artifact_location = root.as_uri()
        exp_id = mlflow.create_experiment(experiment, artifact_location=artifact_location)
    else:
        exp_id = exp.experiment_id
    mlflow.set_experiment(experiment_id=exp_id)
    return exp_id


class QuantileForecaster(mlflow.pyfunc.PythonModel):
    """pyfunc wrapper: mart_features rows in, p10/p50/p90 out."""

    def load_context(self, context):
        self.model = load_model(Path(context.artifacts["model_dir"]))

    def predict(self, context, model_input: pd.DataFrame, params=None) -> pd.DataFrame:
        return self.model.predict(add_derived_features(model_input))


def log_quantile_model(
    model: QuantileLGBM, sample: pd.DataFrame | None = None, name: str = "model"
) -> str:
    """Log ``model`` as a pyfunc (models-from-code) under the active run.

    ``sample`` (a few mart_features rows) is used to record the signature.
    Returns the model URI.
    """
    signature = None
    if sample is not None:
        from mlflow.models import infer_signature

        signature = infer_signature(sample, model.predict(add_derived_features(sample)))
    with tempfile.TemporaryDirectory() as tmp:
        model_dir = Path(tmp) / "model_dir"
        save_model(model, model_dir)
        info = mlflow.pyfunc.log_model(
            name=name,
            python_model=str(Path(__file__).with_name("pyfunc_model.py")),
            artifacts={"model_dir": str(model_dir)},
            signature=signature,
        )
    return info.model_uri


def load_registered(alias: str = CHAMPION) -> tuple[QuantileLGBM, str]:
    """The model behind a registry alias, plus its version string."""
    client = MlflowClient()
    mv = client.get_model_version_by_alias(REGISTERED_MODEL, alias)
    pyfunc = mlflow.pyfunc.load_model(f"models:/{REGISTERED_MODEL}@{alias}")
    return pyfunc.unwrap_python_model().model, mv.version


def export_registered(out_dir: Path, alias: str = CHAMPION) -> dict:
    """Freeze the model behind ``alias`` into ``out_dir``, loadable without MLflow.

    Writes the model files plus ``export.json`` (version, training cut-off and a
    SHA-256 of every file), which is what the public track record cites.
    """
    import hashlib
    import json
    from datetime import UTC, datetime

    from elecprice.modelling.models import save_model

    client = MlflowClient()
    mv = client.get_model_version_by_alias(REGISTERED_MODEL, alias)
    model, version = load_registered(alias)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_model(model, out_dir)
    files = {
        str(p.relative_to(out_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(out_dir.rglob("*"))
        if p.is_file() and p.name != "export.json"
    }
    meta = {
        "registered_model": REGISTERED_MODEL,
        "alias": alias,
        "version": str(version),
        "run_id": mv.run_id,
        "trained_through": mv.tags.get("trained_through"),
        "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sha256": files,
    }
    (out_dir / "export.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta
