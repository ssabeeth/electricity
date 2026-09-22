"""GB day-ahead electricity price forecasting with point-in-time features."""

import os

# MLflow prints an advisory banner on import; keep CLI and Airflow logs clean.
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

__version__ = "0.1.0"
