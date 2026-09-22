"""MLflow models-from-code entry point for the registered forecaster.

MLflow executes this file when loading the model, so no pickled Python objects
are stored in the registry; the LightGBM boosters and calibration shifts are
plain artifacts.
"""

import mlflow

from elecprice.modelling.tracking import QuantileForecaster

mlflow.models.set_model(QuantileForecaster())
