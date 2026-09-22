"""Probabilistic forecast metrics.

* Pinball (quantile) loss per quantile and averaged over quantiles. Lower is
  better and it is a proper scoring rule, so it rewards honest quantiles.
* Coverage of the P10-P90 interval (nominal 80%).
* MAE and RMSE of the P50.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def pinball_loss(y: np.ndarray, q_pred: np.ndarray, alpha: float) -> float:
    y = np.asarray(y, dtype=float)
    q_pred = np.asarray(q_pred, dtype=float)
    diff = y - q_pred
    return float(np.mean(np.maximum(alpha * diff, (alpha - 1.0) * diff)))


def qcol(q: float) -> str:
    return f"p{round(q * 100):02d}"


def evaluate(y: pd.Series, preds: pd.DataFrame, quantiles=(0.1, 0.5, 0.9)) -> dict[str, float]:
    """Metrics for one model over rows where the target is known."""
    mask = y.notna().to_numpy()
    yv = y.to_numpy(dtype=float)[mask]
    if len(yv) == 0:
        return {"n": 0}
    out: dict[str, float] = {"n": len(yv)}
    losses = []
    for q in quantiles:
        loss = pinball_loss(yv, preds[qcol(q)].to_numpy(dtype=float)[mask], q)
        out[f"pinball_{qcol(q)}"] = loss
        losses.append(loss)
    out["pinball_mean"] = float(np.mean(losses))
    p50 = preds["p50"].to_numpy(dtype=float)[mask]
    err = yv - p50
    out["mae_p50"] = float(np.mean(np.abs(err)))
    out["rmse_p50"] = float(np.sqrt(np.mean(err**2)))
    out["bias_p50"] = float(np.mean(-err))
    lo, hi = qcol(min(quantiles)), qcol(max(quantiles))
    lo_v = preds[lo].to_numpy(dtype=float)[mask]
    hi_v = preds[hi].to_numpy(dtype=float)[mask]
    out["coverage"] = float(np.mean((yv >= lo_v) & (yv <= hi_v)))
    out["interval_width"] = float(np.mean(hi_v - lo_v))
    for q in quantiles:
        out[f"frac_below_{qcol(q)}"] = float(np.mean(yv <= preds[qcol(q)].to_numpy()[mask]))
    return out
