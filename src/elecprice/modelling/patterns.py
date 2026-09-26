"""Patterns in the data that the modelling choices rest on: ``elec patterns``.

Everything here is computed on delivery days before the first hold-out month
(``selection_end``): the training history and the selection folds. The
hold-out months are not read, so nothing in this report can have steered a
choice towards them. Each pattern ends with what it implies for the model;
``reports/experiments.md`` then tests those implications, one experiment per
pattern.

Writes ``reports/data_patterns.md``, ``reports/data_patterns.json`` and
``reports/figures/patterns_*.png``.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from elecprice.config import REPO_ROOT, get_settings
from elecprice.modelling.data import FEATURES, TARGET, ModelConfig, load_frame
from elecprice.modelling.metrics import qcol

CALENDAR = {"local_hour", "day_of_week", "is_weekend", "is_holiday", "month", "day_of_year"}
HOUR_GROUPS = {
    "night (23-06)": [23, 0, 1, 2, 3, 4, 5, 6],
    "morning (07-10)": [7, 8, 9, 10],
    "midday (11-15)": [11, 12, 13, 14, 15],
    "evening peak (16-19)": [16, 17, 18, 19],
    "late evening (20-22)": [20, 21, 22],
}


def selection_end(config: ModelConfig) -> pd.Timestamp:
    """First day of the first hold-out month: patterns and experiments stop before it."""
    first = pd.Period(config.backtest["first_test_month"], freq="M")
    return (first + int(config.backtest["selection_folds"])).start_time


def hour_group(hour: pd.Series) -> pd.Series:
    lookup = {h: g for g, hours in HOUR_GROUPS.items() for h in hours}
    return hour.astype(int).map(lookup)


def _tails(y: pd.Series) -> dict:
    q = y.quantile([0.01, 0.1, 0.5, 0.9, 0.99])
    return {
        "half_hours": len(y),
        "mean": float(y.mean()),
        "skew": float(y.skew()),
        "excess_kurtosis": float(y.kurt()),
        "quantiles": {f"q{int(k * 100):02d}": float(v) for k, v in q.items()},
        "max": float(y.max()),
        "negative_share": float((y < 0).mean()),
        "above_300_share": float((y > 300).mean()),
        "mean_minus_median": float(y.mean() - y.median()),
    }


def _merit_order(df: pd.DataFrame) -> list[dict]:
    """Price against the forecast residual demand, quarter by quarter (OLS)."""
    rows = []
    for q, g in df.dropna(subset=["residual_demand_mw"]).groupby(
        df["settlement_date"].dt.to_period("Q")
    ):
        x, y = g["residual_demand_mw"] / 1000, g[TARGET]
        slope = float(np.cov(x, y)[0, 1] / x.var())
        rows.append(
            {
                "quarter": str(q),
                "half_hours": len(g),
                "slope_per_gw": slope,
                "price_at_20gw": float(y.mean() + slope * (20 - x.mean())),
                "spearman": float(g[[TARGET, "residual_demand_mw"]].corr("spearman").iloc[0, 1]),
                "gas_share": float(g["gas_share_7d"].mean()),
            }
        )
    return rows


def _levels(df: pd.DataFrame) -> list[dict]:
    g = df.groupby(df["settlement_date"].dt.to_period("M"))[TARGET]
    return [{"month": str(m), "mean": float(s.mean()), "std": float(s.std())} for m, s in g]


def _profile(df: pd.DataFrame) -> dict:
    """The daily shape, and how well last week's shape predicts this week's."""
    weekend = df["is_weekend"].astype(bool) | df["is_holiday"].astype(bool)
    shape = df.groupby([weekend.rename("weekend"), df["local_hour"].astype(int)])[TARGET].mean()
    day = df.assign(dev=df[TARGET] - df.groupby("settlement_date")[TARGET].transform("mean"))
    wide = day.pivot_table(index="settlement_date", columns="settlement_period", values="dev")
    wide = wide.reindex(pd.date_range(wide.index.min(), wide.index.max(), freq="D"))
    corr = [
        wide.iloc[i].corr(wide.iloc[i - 7])
        for i in range(7, len(wide))
        if wide.iloc[i].notna().sum() > 40 and wide.iloc[i - 7].notna().sum() > 40
    ]
    corr2 = [
        wide.iloc[i].corr(wide.iloc[i - 2])
        for i in range(2, len(wide))
        if wide.iloc[i].notna().sum() > 40 and wide.iloc[i - 2].notna().sum() > 40
    ]
    weekday = shape.loc[False]
    return {
        "weekday_by_hour": {int(h): float(v) for h, v in weekday.items()},
        "weekend_by_hour": {int(h): float(v) for h, v in shape.loc[True].items()},
        "evening_peak_premium": float(weekday.loc[16:19].mean() - weekday.mean()),
        "weekend_discount": float(shape.loc[True].mean() - weekday.mean()),
        "shape_corr_same_day_last_week": float(np.median(corr)),
        "shape_corr_two_days_earlier": float(np.median(corr2)),
    }


def _persistence(df: pd.DataFrame) -> list[dict]:
    rows = []
    for col in (
        "price_d1_same_period",
        "price_d2_same_period",
        "price_d7_same_period",
        "price_24h_mean",
        "price_7d_mean",
    ):
        known = df[col].notna()
        rows.append(
            {
                "feature": col,
                "known_share": float(known.mean()),
                "spearman": float(df.loc[known, [TARGET, col]].corr("spearman").iloc[0, 1]),
                "mae_as_forecast": float((df.loc[known, TARGET] - df.loc[known, col]).abs().mean()),
            }
        )
    return rows


def _renewables(df: pd.DataFrame) -> list[dict]:
    """Price by the forecast share of demand met by wind and embedded solar."""
    supply = df["windfor_mw"] + df["emb_wind_mw"] + df["emb_solar_mw"]
    share = supply / (df["ndf_demand_mw"] + df["emb_wind_mw"] + df["emb_solar_mw"])
    bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 1.5]
    b = pd.cut(share, bins, right=False)
    g = df.groupby(b, observed=True)[TARGET]
    return [
        {
            "renewable_share": f"{iv.left:.0%}-{iv.right:.0%}"
            if iv.right < 1
            else f"{iv.left:.0%}+",
            "half_hours": int(s.size),
            "median_price": float(s.median()),
            "negative_share": float((s < 0).mean()),
            "p10_to_p90": float(s.quantile(0.9) - s.quantile(0.1)),
        }
        for iv, s in g
    ]


def _volatility(df: pd.DataFrame) -> dict:
    # log, because a single spike dominates a day's standard deviation
    daily = np.log(df.groupby("settlement_date")[TARGET].std())
    monthly = df.groupby(df["settlement_date"].dt.to_period("M"))[TARGET].std()
    return {
        "daily_log_std_autocorr_lag1": float(daily.autocorr(1)),
        "daily_log_std_autocorr_lag7": float(daily.autocorr(7)),
        "daily_log_std_autocorr_lag28": float(daily.autocorr(28)),
        "monthly_std_min": float(monthly.min()),
        "monthly_std_max": float(monthly.max()),
    }


def _errors_by_hour(pred_path: Path, folds: int, quantiles) -> list[dict] | None:
    """Where the current model misses, on the selection folds of the saved backtest."""
    if not pred_path.exists():
        return None
    p = pd.read_parquet(pred_path)
    p = p[(p["model"] == "lgbm_quantile") & (p["fold"] <= folds) & p[TARGET].notna()]
    hour = pd.to_datetime(p["start_time_utc"]).dt.tz_localize("UTC").dt.tz_convert("Europe/London")
    p = p.assign(group=hour_group(hour.dt.hour))
    lo, hi = qcol(min(quantiles)), qcol(max(quantiles))
    rows = []
    for g, part in p.groupby("group", sort=False):
        y = part[TARGET]
        rows.append(
            {
                "hours": g,
                "half_hours": len(part),
                "coverage": float(((y >= part[lo]) & (y <= part[hi])).mean()),
                "below_p10": float((y < part[lo]).mean()),
                "above_p90": float((y > part[hi]).mean()),
                "mae_p50": float((y - part["p50"]).abs().mean()),
            }
        )
    order = list(HOUR_GROUPS)
    return sorted(rows, key=lambda r: order.index(r["hours"]))


def _span(df: pd.DataFrame, fmt: str) -> str:
    d = df["settlement_date"]
    return f"{d.min():{fmt}} to {d.max():{fmt}}"


def _adversarial(df: pd.DataFrame, split: pd.Timestamp) -> dict:
    """Can a classifier tell the first training year from the selection year?

    Every third calendar month is held out whole. Holding out days is not enough:
    the 7-day inputs of a held-out day are almost those of its neighbours, and
    the classifier then scores ROC-AUC 1.00 by recognising the week.
    """
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score

    feats = [f for f in FEATURES if f not in CALENDAR]
    y = (df["settlement_date"] >= split).astype(int).to_numpy()
    test = (df["settlement_date"].dt.month % 3 == 0).to_numpy()

    def fit(cols: list[str]):
        clf = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15, verbose=-1)
        clf.fit(df.loc[~test, cols], y[~test])
        return clf, float(roc_auc_score(y[test], clf.predict_proba(df.loc[test, cols])[:, 1]))

    clf, auc = fit(feats)
    imp = pd.Series(clf.booster_.feature_importance("gain"), index=feats)
    imp = (imp / imp.sum()).sort_values(ascending=False)
    # slow system state that marks the year; price levels are left to the target design
    markers = [f for f, v in imp.head(6).items() if not f.startswith("price_") and v > 0.05]
    _, auc_without = fit([f for f in feats if f not in markers])
    first, later = df[y == 0], df[y == 1]
    return {
        "first_period": _span(first, "%Y-%m"),
        "second_period": _span(later, "%Y-%m"),
        "auc": auc,
        "top": [
            {
                "feature": f,
                "gain_share": float(v),
                "first_mean": float(first[f].mean()),
                "second_mean": float(later[f].mean()),
            }
            for f, v in imp.head(6).items()
        ],
        "auc_without_markers": auc_without,
        "markers": markers,
    }


def compute(df: pd.DataFrame, config: ModelConfig, pred_path: Path) -> dict:
    end = selection_end(config)
    df = df[(df["settlement_date"] < end) & df[TARGET].notna()].copy()
    first_test = pd.Period(config.backtest["first_test_month"], freq="M").start_time
    return {
        "period": _span(df, "%Y-%m-%d"),
        "holdout_from": f"{end:%Y-%m-%d}",
        "tails": _tails(df[TARGET]),
        "levels": _levels(df),
        "merit_order": _merit_order(df),
        "profile": _profile(df),
        "persistence": _persistence(df),
        "renewables": _renewables(df),
        "volatility": _volatility(df),
        "errors_by_hour": _errors_by_hour(
            pred_path, int(config.backtest["selection_folds"]), config.quantiles
        ),
        "adversarial": _adversarial(df, first_test),
        "missing": {f: float(df[f].isna().mean()) for f in FEATURES if df[f].isna().mean() > 0.001},
    }


def figures(res: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    lv = pd.DataFrame(res["levels"])
    mo = pd.DataFrame(res["merit_order"])
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.2))
    ax[0].plot(lv["month"], lv["mean"], marker="o", ms=3, label="monthly mean")
    ax[0].plot(lv["month"], lv["std"], marker="o", ms=3, color="#d9822b", label="monthly std")
    ax[0].set_ylabel("£/MWh")
    ax[0].set_title("Level and volatility drift")
    ax[0].tick_params(axis="x", rotation=70)
    ax[0].legend(frameon=False)
    ax[1].plot(mo["quarter"], mo["price_at_20gw"], marker="o", ms=3, label="price at 20 GW")
    ax2 = ax[1].twinx()
    ax2.plot(
        mo["quarter"], mo["slope_per_gw"], marker="s", ms=3, color="#d9822b", label="£/MWh per GW"
    )
    ax[1].set_title("The merit order moves: price at 20 GW residual demand, and slope")
    ax[1].set_ylabel("£/MWh at 20 GW")
    ax2.set_ylabel("slope, £/MWh per GW")
    ax[1].tick_params(axis="x", rotation=70)
    fig.tight_layout()
    fig.savefig(out / "patterns_level.png")
    plt.close(fig)

    pr = res["profile"]
    fig, ax = plt.subplots(figsize=(6, 3))
    for key, label in (("weekday_by_hour", "weekday"), ("weekend_by_hour", "weekend or holiday")):
        s = pd.Series(pr[key]).sort_index()
        ax.plot(s.index, s.values, marker="o", ms=3, label=label)
    ax.set_xlabel("UK local hour")
    ax.set_ylabel("mean price, £/MWh")
    ax.set_title("Daily shape")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(out / "patterns_profile.png")
    plt.close(fig)

    rn = pd.DataFrame(res["renewables"])
    fig, ax = plt.subplots(figsize=(6, 3))
    ax.bar(rn["renewable_share"], rn["median_price"], color="#2a6fdb")
    ax.set_ylabel("median price, £/MWh")
    ax.set_xlabel("forecast wind + solar share of demand")
    ax2 = ax.twinx()
    ax2.plot(rn["renewable_share"], rn["negative_share"] * 100, color="#d9822b", marker="o", ms=3)
    ax2.set_ylabel("% of half-hours below £0")
    ax.set_title("Renewables push the price down, and below zero")
    fig.tight_layout()
    fig.savefig(out / "patterns_renewables.png")
    plt.close(fig)

    if res["errors_by_hour"]:
        er = pd.DataFrame(res["errors_by_hour"])
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.bar(er["hours"], er["below_p10"] * 100, label="below P10", color="#2a6fdb")
        ax.bar(
            er["hours"],
            er["above_p90"] * 100,
            bottom=er["below_p10"] * 100,
            label="above P90",
            color="#d9822b",
        )
        ax.axhline(20, color="k", lw=0.8, ls="--")
        ax.set_ylabel("% of outcomes outside P10-P90")
        ax.set_title("Current model: misses by time of day (nominal 20%)")
        ax.tick_params(axis="x", rotation=20)
        ax.legend(frameon=False)
        fig.tight_layout()
        fig.savefig(out / "patterns_errors.png")
        plt.close(fig)


def _pct(x: float) -> str:
    return f"{x:.1%}"


def markdown(res: dict) -> str:
    t, pr, vo, ad = res["tails"], res["profile"], res["volatility"], res["adversarial"]
    mo, lv = res["merit_order"], res["levels"]
    lo_m, hi_m = min(lv, key=lambda r: r["mean"]), max(lv, key=lambda r: r["mean"])
    lo_q, hi_q = (
        min(mo, key=lambda r: r["price_at_20gw"]),
        max(mo, key=lambda r: r["price_at_20gw"]),
    )
    sl = [r["slope_per_gw"] for r in mo]
    pers = {r["feature"]: r for r in res["persistence"]}
    rn = res["renewables"]
    lines = [
        "# Data patterns and what they imply",
        "",
        f"_Generated by `elec patterns`. Delivery days {res['period']}: the training history "
        f"and the selection folds. The hold-out months (from {res['holdout_from']}) are not "
        "read._",
        "",
        "Each section states a pattern, the evidence, and what it implies for the model. "
        "[experiments.md](experiments.md) tests every implication that is a change to the "
        "current model, under a rule fixed before any experiment ran.",
        "",
        "## 1. Heavy tails and negative prices",
        "",
        f"Over {t['half_hours']:,} half-hours the price has skew {t['skew']:.1f} and excess "
        f"kurtosis {t['excess_kurtosis']:.0f}. The middle 80% runs from "
        f"£{t['quantiles']['q10']:.0f} to £{t['quantiles']['q90']:.0f}/MWh, but the maximum is "
        f"£{t['max']:,.0f}, {_pct(t['negative_share'])} of half-hours are below zero, and the "
        f"mean sits £{abs(t['mean_minus_median']):.1f} "
        f"{'above' if t['mean_minus_median'] > 0 else 'below'} the median: the spikes and the "
        "negative prices pull in opposite directions.",
        "",
        "**Implies:** a squared-error model would chase the few spikes. Quantile (pinball) loss "
        "is robust to them and gives the P10 and P90 the battery and the reader need, so the "
        "model predicts quantiles and is scored on pinball loss. Intervals from a symmetric "
        "error model would be wrong on both sides; the experiments include a point model with "
        "empirical residual intervals to check that direct quantile models earn their keep.",
        "",
        "## 2. The level drifts, and the merit order moves with it",
        "",
        f"Monthly mean prices range from £{lo_m['mean']:.0f} ({lo_m['month']}) to "
        f"£{hi_m['mean']:.0f} ({hi_m['month']}). The level is set by the fuel cost of the "
        "marginal plant, which is mostly gas, and there is no free point-in-time gas price. The "
        "clearest trace of it is the price at a fixed residual demand: at 20 GW it rose from "
        f"£{lo_q['price_at_20gw']:.0f} ({lo_q['quarter']}) to £{hi_q['price_at_20gw']:.0f} "
        f"({hi_q['quarter']}), and the slope of price against residual demand ranged from "
        f"£{min(sl):.1f} to £{max(sl):.1f}/MWh per GW.",
        "",
        "![level](figures/patterns_level.png)",
        "",
        "| Quarter | Half-hours | Price at 20 GW | Slope per GW "
        "| Spearman with residual demand | Gas share |",
        "|---|---|---|---|---|---|",
        *[
            f"| {r['quarter']} | {r['half_hours']:,} | £{r['price_at_20gw']:.1f} | "
            f"£{r['slope_per_gw']:.2f} | {r['spearman']:.2f} | {r['gas_share']:.0%} |"
            for r in mo
        ],
        "",
        "**Implies:** trees cannot extrapolate a level they have not seen, which is why the "
        "model already learns the price minus its trailing 7-day mean. Three experiments follow "
        "from the moving curve: a *recent merit order* feature (the price the last two weeks' "
        "curve implies for tomorrow's residual demand), *recency weighting* of training rows, "
        "and a *rolling one-year window* instead of the expanding one.",
        "",
        "## 3. Residual demand is the strongest single signal",
        "",
        "Rank correlation with the price (Spearman), over the same period: residual demand "
        f"{np.mean([r['spearman'] for r in mo]):.2f} on average across quarters, against "
        f"{pers['price_d2_same_period']['spearman']:.2f} for the same half-hour two days earlier "
        f"and {pers['price_d7_same_period']['spearman']:.2f} for last week's. Within every "
        "quarter the price rises with residual demand.",
        "",
        "**Implies:** residual demand (the NESO demand forecast minus the wind forecast) stays "
        "the core input, and the relationship is monotone, so an experiment constrains the "
        "trees to be *monotone in residual demand*, which should help most where the training "
        "data is thin (the extremes).",
        "",
        "## 4. The daily shape repeats",
        "",
        f"On weekdays the 16:00-19:59 evening peak averages £{pr['evening_peak_premium']:.1f} "
        f"above the daily mean; weekends and holidays average £{-pr['weekend_discount']:.1f} "
        "below weekdays. The shape of a day (each half-hour minus the day's mean) correlates "
        f"with the same weekday's shape a week earlier at a median of "
        f"{pr['shape_corr_same_day_last_week']:.2f}, and with two days earlier at "
        f"{pr['shape_corr_two_days_earlier']:.2f}.",
        "",
        "![profile](figures/patterns_profile.png)",
        "",
        "**Implies:** the calendar features and the D-2 and D-7 same-period lags already carry "
        "the shape. One experiment adds a smoother version, the *same half-hour's mean over "
        "the last seven known days*, which is less noisy than any single lag.",
        "",
        "## 5. Persistence: which lags carry information",
        "",
        "| Lag | Known at the cutoff | Spearman with price | MAE used as a forecast |",
        "|---|---|---|---|",
        *[
            f"| `{r['feature']}` | {_pct(r['known_share'])} | {r['spearman']:.2f} | "
            f"£{r['mae_as_forecast']:.2f} |"
            for r in res["persistence"]
        ],
        "",
        "At a 09:00 cutoff only the early-morning half-hours of D-1 are known, so the D-1 "
        "lag is missing for most rows by design. The same half-hour two days earlier is a "
        "better naive forecast than a week earlier.",
        "",
        "**Implies:** the brief's baseline (same half-hour last week) is kept as the reference, "
        "and the model comparison adds the D-2 naive as a second, stronger naive, so the "
        "model's skill is not flattered by a weak baseline.",
        "",
        "## 6. Renewables set the bottom of the distribution",
        "",
        "| Forecast wind + solar share of demand | Half-hours | Median price | Below £0 "
        "| P90 - P10 |",
        "|---|---|---|---|---|",
        *[
            f"| {r['renewable_share']} | {r['half_hours']:,} | £{r['median_price']:.1f} | "
            f"{_pct(r['negative_share'])} | £{r['p10_to_p90']:.1f} |"
            for r in rn
        ],
        "",
        "![renewables](figures/patterns_renewables.png)",
        "",
        "**Implies:** the model sees the wind, solar and demand forecasts separately; negative "
        "prices come from their ratio. An experiment adds the *forecast renewable share* "
        "directly, so a tree can split on it in one step.",
        "",
        "## 7. Volatility persists for days, not weeks",
        "",
        "The log of a day's price standard deviation (log, because one spike dominates a "
        "day's standard deviation) correlates with the previous day's at "
        f"{vo['daily_log_std_autocorr_lag1']:.2f}, with a week earlier at "
        f"{vo['daily_log_std_autocorr_lag7']:.2f} and with four weeks earlier at "
        f"{vo['daily_log_std_autocorr_lag28']:.2f}. Monthly standard deviations range from "
        f"£{vo['monthly_std_min']:.0f} to £{vo['monthly_std_max']:.0f}.",
        "",
        "**Implies:** the width of tomorrow's interval should follow the last few days, which "
        "the model sees through `price_7d_std`, while the conformal calibration averages the "
        "last 56 days. An experiment tries a *28-day calibration window*, which follows "
        "volatility faster but estimates each shift from half the data.",
        "",
    ]
    if res["errors_by_hour"]:
        worst = max(
            res["errors_by_hour"],
            key=lambda r: abs(r["below_p10"] - 0.1) + abs(r["above_p90"] - 0.1),
        )
        side = "below the P10" if worst["below_p10"] > worst["above_p90"] else "above the P90"
        share = max(worst["below_p10"], worst["above_p90"])
        lines += [
            "## 8. Where the current model misses",
            "",
            "On the selection folds of the saved backtest, by UK time of day:",
            "",
            "| Hours | Half-hours | P10-P90 coverage | Below P10 | Above P90 | MAE P50 |",
            "|---|---|---|---|---|---|",
            *[
                f"| {r['hours']} | {r['half_hours']:,} | {_pct(r['coverage'])} | "
                f"{_pct(r['below_p10'])} | {_pct(r['above_p90'])} | £{r['mae_p50']:.2f} |"
                for r in res["errors_by_hour"]
            ],
            "",
            "![errors](figures/patterns_errors.png)",
            "",
            f"The two tails are most unbalanced in the {worst['hours']}: {_pct(share)} of "
            f"outcomes fall {side} (nominal 10%). One calibration shift per quantile cannot "
            "fix a miss that depends on the time of day.",
            "",
            "**Implies:** an experiment estimates the conformal shifts *per time-of-day group* "
            "instead of once.",
            "",
        ]
    lines += [
        "## 9. The inputs drift between years",
        "",
        f"A classifier trained to tell {ad['first_period']} from {ad['second_period']} on the "
        "non-calendar inputs, with every third calendar month held out whole, reaches ROC-AUC "
        f"{ad['auc']:.2f} (0.5 would mean the years look alike). The inputs that give the "
        "year away:",
        "",
        "| Feature | Share of the classifier's gain | First year mean | Second year mean |",
        "|---|---|---|---|",
        *[
            f"| `{r['feature']}` | {_pct(r['gain_share'])} | {r['first_mean']:,.3g} "
            f"| {r['second_mean']:,.3g} |"
            for r in ad["top"]
        ],
        "",
        "The non-price inputs among them with more than 5% of the gain are slow-moving "
        "system state: " + ", ".join(f"`{m}`" for m in ad["markers"]) + ". Without them "
        f"the classifier's ROC-AUC falls to {ad['auc_without_markers']:.2f}. (Holding out "
        "single days instead gives 1.00, because a held-out day's 7-day inputs are nearly "
        "its neighbours'.)",
        "",
        "**Implies:** a tree can use slow system state as a marker of the year rather than "
        "as a cause of the price. An experiment *drops these regime markers*. The drift in "
        "price levels is what the de-levelled target and the relative price lags already "
        "handle.",
        "",
        "## Missing values",
        "",
        "Inputs missing on more than 0.1% of half-hours (LightGBM routes missing values "
        "down a learned branch, so nothing is imputed):",
        "",
        "| Feature | Missing |",
        "|---|---|",
        *[
            f"| `{f}` | {_pct(v)} |"
            for f, v in sorted(res["missing"].items(), key=lambda kv: -kv[1])
        ],
        "",
    ]
    return "\n".join(lines)


def run(out_dir: Path | None = None) -> dict:
    out_dir = out_dir or REPO_ROOT / "reports"
    config = ModelConfig.load()
    pred = get_settings().outputs_dir / "backtest" / "predictions.parquet"
    res = compute(load_frame(), config, pred)
    (out_dir / "data_patterns.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
    figures(res, out_dir / "figures")
    (out_dir / "data_patterns.md").write_text(markdown(res))
    return res
