"""Load the modelling frame: point-in-time features joined to realised prices."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml

from elecprice.config import get_settings

KEYS = ["settlement_date", "settlement_period", "start_time_utc", "cutoff_utc"]
TARGET = "price_gbp_mwh"

BASE_FEATURES = [
    # calendar
    "local_hour",
    "day_of_week",
    "is_weekend",
    "is_holiday",
    "month",
    "day_of_year",
    # NESO forecasts
    "ndf_demand_mw",
    "windfor_mw",
    "residual_demand_mw",
    "emb_wind_mw",
    "emb_solar_mw",
    "emb_solar_load_factor",
    # weather forecasts
    "wx_wind_speed_100m_ms",
    "wx_wind_power_index",
    "wx_solar_radiation_wm2",
    "wx_cloud_cover_pct",
    "wx_temperature_c",
    # lagged prices
    "price_d7_same_period",
    "price_d2_same_period",
    "price_d1_same_period",
    "price_last_known",
    "price_24h_mean",
    "price_7d_mean",
    "price_7d_std",
    "price_7d_min",
    "price_7d_max",
    # recent system state
    "ci_actual_24h_mean",
    "gas_share_7d",
    "wind_share_7d",
    "nuclear_share_7d",
    "net_imports_7d_mean_mw",
    "demand_outturn_7d_mean_mw",
]

# Level-free versions of the price lags (relative to the trailing 7-day mean).
RELATIVE_PRICE_FEATURES = {
    "price_d7_rel": "price_d7_same_period",
    "price_d2_rel": "price_d2_same_period",
    "price_d1_rel": "price_d1_same_period",
    "price_last_rel": "price_last_known",
    "price_24h_rel": "price_24h_mean",
}

FEATURES = BASE_FEATURES + list(RELATIVE_PRICE_FEATURES)


@dataclass(frozen=True)
class ModelConfig:
    quantiles: tuple[float, ...] = (0.1, 0.5, 0.9)
    target_mode: str = "delta_7d_mean"
    calibration: dict[str, Any] = field(default_factory=dict)
    lightgbm: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    backtest: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None, **overrides: Any) -> ModelConfig:
        path = path or get_settings().config_dir / "model.yaml"
        raw = yaml.safe_load(Path(path).read_text())
        raw.update({k: v for k, v in overrides.items() if v is not None})
        raw["quantiles"] = tuple(raw["quantiles"])
        return cls(**raw)

    def to_flat_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "quantiles": ",".join(str(q) for q in self.quantiles),
            "target_mode": self.target_mode,
        }
        for section in ("calibration", "lightgbm", "baseline", "backtest"):
            for k, v in getattr(self, section).items():
                out[f"{section}.{k}"] = v
        return out


def add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for new, col in RELATIVE_PRICE_FEATURES.items():
        out[new] = out[col] - out["price_7d_mean"]
    for col in ("is_weekend", "is_holiday"):
        out[col] = out[col].astype("float64")
    return out


def load_frame(
    warehouse: Path | None = None,
    start: str | None = None,
    end: str | None = None,
    *,
    with_target: bool = True,
) -> pd.DataFrame:
    """Features (and optionally the realised price) for delivery days in [start, end]."""
    warehouse = warehouse or get_settings().warehouse_path
    where = []
    if start:
        where.append(f"f.settlement_date >= date '{start}'")
    if end:
        where.append(f"f.settlement_date <= date '{end}'")
    clause = ("where " + " and ".join(where)) if where else ""
    target = ", a.price_gbp_mwh" if with_target else ""
    join = (
        "left join marts.fct_price_actuals a using (settlement_date, settlement_period)"
        if with_target
        else ""
    )
    sql = f"""
        select f.*{target}
        from marts.mart_features f
        {join}
        {clause}
        order by f.start_time_utc
    """
    with duckdb.connect(str(warehouse), read_only=True) as con:
        df = con.sql(sql).df()
    df["settlement_date"] = pd.to_datetime(df["settlement_date"])
    return add_derived_features(df)
