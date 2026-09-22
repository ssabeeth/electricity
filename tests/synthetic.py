"""Synthetic feature frames with the mart's columns, for fast model tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

from elecprice.modelling.data import BASE_FEATURES, add_derived_features


def synthetic_frame(days: int = 200, start: str = "2024-03-01", seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=days * 48, freq="30min")
    n = len(idx)
    hour = idx.hour + idx.minute / 60
    wind = rng.gamma(2.0, 4.0, n)
    daily = 20 * np.sin((hour - 7) / 24 * 2 * np.pi)
    noise_scale = 5 + 2 * wind  # heteroscedastic: windy periods are noisier
    price = 80 + daily - 2.5 * wind + rng.normal(0, 1, n) * noise_scale
    df = pd.DataFrame(index=range(n))
    for col in BASE_FEATURES:
        df[col] = rng.normal(0, 1, n)
    df["settlement_date"] = idx.normalize()
    df["settlement_period"] = (idx.hour * 2 + idx.minute // 30 + 1).astype(int)
    df["start_time_utc"] = idx
    df["cutoff_utc"] = idx.normalize() - pd.Timedelta(hours=15)  # 09:00 on D-1
    df["local_hour"] = hour
    df["day_of_week"] = idx.dayofweek + 1
    df["is_weekend"] = df["day_of_week"] >= 6
    df["is_holiday"] = False
    df["wx_wind_speed_100m_ms"] = wind
    df["price_gbp_mwh"] = price
    s = pd.Series(price)
    df["price_d7_same_period"] = s.shift(7 * 48)
    df["price_d2_same_period"] = s.shift(2 * 48)
    df["price_d1_same_period"] = np.nan
    df["price_7d_mean"] = s.shift(2 * 48).rolling(7 * 48, min_periods=48).mean()
    df["price_last_known"] = s.shift(30)
    df["price_24h_mean"] = s.shift(30).rolling(48).mean()
    return add_derived_features(df)
