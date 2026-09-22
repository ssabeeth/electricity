import numpy as np
import pandas as pd
import pytest

from elecprice.modelling.metrics import evaluate, pinball_loss, qcol


def test_pinball_loss_known_values():
    y = np.array([10.0, 10.0])
    # under-forecast by 2 at alpha=0.9 costs 0.9*2; over-forecast by 2 costs 0.1*2
    assert pinball_loss(y, np.array([8.0, 8.0]), 0.9) == pytest.approx(1.8)
    assert pinball_loss(y, np.array([12.0, 12.0]), 0.9) == pytest.approx(0.2)
    # at the median pinball is half the absolute error
    assert pinball_loss(y, np.array([7.0, 13.0]), 0.5) == pytest.approx(1.5)


def test_pinball_is_minimised_by_the_true_quantile():
    rng = np.random.default_rng(1)
    y = rng.normal(0, 1, 20000)
    grid = np.linspace(-2, 2, 81)
    losses = [pinball_loss(y, np.full_like(y, g), 0.9) for g in grid]
    assert grid[int(np.argmin(losses))] == pytest.approx(np.quantile(y, 0.9), abs=0.06)


def test_evaluate_coverage_and_ignores_missing_targets():
    y = pd.Series([1.0, 2.0, 3.0, np.nan])
    preds = pd.DataFrame(
        {"p10": [0.0, 2.5, 2.0, 0], "p50": [1.0, 3.0, 3.0, 0], "p90": [2.0, 3.5, 4.0, 0]}
    )
    m = evaluate(y, preds)
    assert m["n"] == 3
    assert m["coverage"] == pytest.approx(2 / 3)
    assert m["mae_p50"] == pytest.approx(1 / 3)
    assert m["interval_width"] == pytest.approx((2 + 1 + 2) / 3)


def test_qcol():
    assert [qcol(q) for q in (0.1, 0.5, 0.9)] == ["p10", "p50", "p90"]
