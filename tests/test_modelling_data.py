import pytest

from elecprice.modelling.data import FEATURES, ModelConfig, load_frame

pytestmark = pytest.mark.slow


def test_load_frame_on_fixture_warehouse(fixture_warehouse):
    df = load_frame(fixture_warehouse)
    assert set(FEATURES) <= set(df.columns)
    assert "price_gbp_mwh" in df.columns
    assert df["price_gbp_mwh"].notna().mean() > 0.99
    expected = df["price_d7_same_period"] - df["price_7d_mean"]
    known = expected.notna()
    assert known.mean() > 0.95
    assert (df.loc[known, "price_d7_rel"] - expected[known]).abs().max() < 1e-9


def test_model_config_loads_and_flattens():
    cfg = ModelConfig.load()
    flat = cfg.to_flat_dict()
    assert cfg.quantiles == (0.1, 0.5, 0.9)
    assert flat["target_mode"] in {"level", "delta_7d_mean"}
    assert "lightgbm.n_estimators" in flat
    assert ModelConfig.load(target_mode="level").target_mode == "level"
